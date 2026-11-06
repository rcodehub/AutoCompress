"""Compression engine.

Every sub-folder of a source folder becomes its own RAR archive. The archive is built in a private work
folder, tested, compared with the folder (file count), given its checksum (in the file name or an SFV file)
and only then moved into place. The folder is deleted / moved only after all of that succeeded, and only
if it did not change while it was being compressed.
"""
import fnmatch
import json
import logging
import logging.handlers
import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
import zlib
from collections import deque
from concurrent.futures import ThreadPoolExecutor

import mounts
from stats import Stats

FUTURE_SLACK = 120                      # seconds a file date may be ahead of the clock before it is not trusted
WORK_PREFIX = ".autocompress-"          # private per-archive work folders
CRC_RE = re.compile(r" \[([0-9A-Fa-f]{8})\]$")
PART_RE = re.compile(r"^(.*)\.part0*(\d+)\.rar$", re.I)
EXISTING_RE = re.compile(r"^(.*?)(?: \[[0-9A-Fa-f]{8}\])?(?:\.part0*1)?\.rar$", re.I)

FAILED_MAX = 5000                       # entries kept in the remembered-state file
TRANSIENT_CODES = (5, 8)                # rar: write error, not enough memory - often a side effect of running in parallel

RAR_CODES = {1: "warning - some files could not be read (in use, or no permission?)", 2: "fatal error",
             3: "checksum error - the archive is damaged", 4: "the archive is locked",
             5: "write error - disk full, or no permission to write?", 6: "could not open a file",
             7: "bad command line", 8: "not enough memory (try a smaller dictionary or fewer folders at once)",
             9: "could not create the archive file", 10: "no files to add", 11: "wrong or missing password",
             255: "stopped"}


class Failed(Exception):
    """A folder could not be compressed. remember=True: don't retry on scheduled scans until it changes.
    transient=True: worth one more try on its own after the other jobs are done."""

    def __init__(self, msg, remember=False, transient=False):
        super().__init__(msg)
        self.remember = remember
        self.transient = transient      # may work when tried again on its own (out of memory, disk full, ...)


def human(n):
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def crc32_file(path, cancel=None):
    """CRC32 of a file as 8 upper-case hex digits (the value tools like QuickSFV / 7-Zip show)."""
    v = 0
    with open(path, "rb") as f:
        while True:
            if cancel and cancel():
                raise Failed("stopped")
            b = f.read(8 << 20)
            if not b:
                break
            v = zlib.crc32(b, v)
    return "%08X" % (v & 0xFFFFFFFF)


def which_tools():
    rar = shutil.which("rar")
    info = {"rar": rar, "rar_version": "", "rar_license": ""}
    if rar:
        try:   # running rar without arguments prints its banner
            out = subprocess.run([rar], capture_output=True, text=True, errors="replace", timeout=15,
                                 stdin=subprocess.DEVNULL).stdout
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            if lines:
                info["rar_version"] = re.sub(r"\s+Copyright.*", "", lines[0])
            if len(lines) > 1:
                info["rar_license"] = lines[1]
        except (OSError, subprocess.SubprocessError):
            pass
    return info


def parse_sfv(path):
    """[(file name, CRC)] from an SFV file."""
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith(";"):
                continue
            parts = ln.rsplit(None, 1)
            if len(parts) == 2 and re.fullmatch(r"[0-9A-Fa-f]{8}", parts[1]):
                out.append((parts[0], parts[1].upper()))
    return out


class Engine:
    def __init__(self, log_dir, history_path):
        self.lock = threading.RLock()
        self.move_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.procs = set()
        self.thread = None
        self.history_path = history_path
        data_dir = os.path.dirname(history_path) or "."
        self.failed_path = os.path.join(data_dir, "failed-folders.json")
        self.failed_lock = threading.Lock()     # one load-modify-save of the remembered state at a time
        self.tools = which_tools()

        self.log_buf = deque(maxlen=2000)
        self.log_seq = 0
        self.log_cleared = 0
        self._file_log = None
        try:
            os.makedirs(log_dir, exist_ok=True)
            handler = logging.handlers.RotatingFileHandler(
                os.path.join(log_dir, "compress.log"), maxBytes=2_000_000, backupCount=3)
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._file_log = logging.getLogger(f"autocompress.{id(self)}")
            self._file_log.propagate = False
            self._file_log.setLevel(logging.INFO)
            self._file_log.addHandler(handler)
        except OSError:
            self._file_log = None

        self.history = self._load_history()
        self.stats = Stats(os.path.join(data_dir, "stats.json"))
        self.status = self._blank_status()
        self._warned = set()
        self._reset_run()

    def _reset_run(self):
        self.found = 0
        self.failures = []
        self.deferred = set()        # folders skipped because they changed recently
        self.waiting = {}            # path -> {"ready_at", "files", "bytes"}: when each of those becomes old enough
        self.skipped = 0             # folders skipped because an archive of that name already exists / failed before
        self.crc_added = 0
        self.cancelled = 0           # folders whose compression was cancelled by the user
        self.verified = {"checked": 0, "ok": 0, "bad": 0, "missing": 0}
        self._slots = []
        self._slot_procs = {}        # slot -> running rar processes of that job (so one job can be cancelled alone)
        self._ex_cache = {}          # out_dir -> archive names already there (lower case)
        self._claimed = set()
        self._mounts = []            # mapped folders (refreshed at the start of every run)

    # ------------------------------------------------------------ logging
    def log(self, level, msg):
        with self.lock:
            self.log_seq += 1
            self.log_buf.append({"id": self.log_seq, "t": time.time(), "level": level, "msg": msg})
        if self._file_log:
            getattr(self._file_log, "warning" if level == "warn" else
                    "error" if level == "error" else "info")(msg)

    def clear_log(self):
        """Empty the on-screen log (the log file in /data/logs is kept)."""
        with self.lock:
            self.log_buf.clear()
            self.log_cleared = self.log_seq

    def log_since(self, after=0, limit=400):
        with self.lock:
            items = [e for e in self.log_buf if e["id"] > after]
        return items[-limit:]

    def _warn_once(self, key, msg):
        if key not in self._warned:
            self._warned.add(key)
            self.log("warn", msg)

    # ------------------------------------------------------------ history
    def _load_history(self):
        try:
            with open(self.history_path) as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        try:
            tmp = self.history_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.history[-100:], f)
            os.replace(tmp, self.history_path)
        except OSError as e:
            self.log("warn", f"Could not save history: {e}")

    def _load_failed(self):
        try:
            with open(self.failed_path) as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_failed(self, d):
        """Write the remembered state: entries whose folder is gone are dropped, the newest FAILED_MAX are kept.
        Call with failed_lock held (use _update_failed)."""
        keep = dict(list({p: v for p, v in d.items() if os.path.exists(p)}.items())[-FAILED_MAX:])
        try:
            tmp = self.failed_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(keep, f)
            os.replace(tmp, self.failed_path)
        except OSError:
            pass

    def _update_failed(self, path, signature=None):
        """Remember `path` with its signature, or forget it (signature None) - atomically, as jobs run in parallel."""
        with self.failed_lock:
            d = self._load_failed()
            if signature is None:
                if d.pop(path, None) is None:
                    return
            else:
                d.pop(path, None)                   # re-insert at the end: the oldest entries go first
                d[path] = signature
            self._save_failed(d)

    # ------------------------------------------------------------ status
    def _blank_status(self):
        return {"state": "idle", "trigger": None, "mode": "run", "dry_run": False,
                "started_at": None, "finished_at": None, "result": None, "message": "",
                "compressed": 0, "files": 0, "bytes_in": 0, "bytes_out": 0, "crc_added": 0, "slots": []}

    @property
    def running(self):
        t = self.thread
        return bool(t and t.is_alive())

    def snapshot(self):
        with self.lock:
            s = dict(self.status)
            now = time.time()
            s["slots"] = [
                ({"n": i + 1, "path": sl["path"], "phase": sl["phase"], "pct": sl["pct"],
                  "elapsed": int(now - sl["t"]), "cancel": bool(sl.get("cancel"))} if sl else {"n": i + 1, "path": None, "elapsed": 0, "cancel": False})
                for i, sl in enumerate(self._slots)
            ] if self.status["state"] != "idle" else []
            s["found"] = self.found
            s["failed"] = sum(1 for f in self.failures if not f.get("warn"))
            s["cancelled"] = self.cancelled
            s["done"] = s["compressed"] + s["failed"] + self.cancelled
            s["remaining"] = max(0, s["found"] - s["done"])
            s["deferred"] = len(self.deferred)
            s["waiting"] = sorted(({"path": k, **v} for k, v in self.waiting.items()), key=lambda w: w["ready_at"])[:30]
            s["waiting_total"] = len(self.waiting)
            s["skipped"] = self.skipped
            s["crc_added"] = self.crc_added
            s["verified"] = dict(self.verified)
            s["failures"] = self.failures[-50:]
            s["tools"] = self.tools
            return s

    # ------------------------------------------------------------ control
    def start(self, cfg, trigger="manual", dry_run=False, verify_only=False, oneoff=None):
        """oneoff = {"path": folder, "mode": "folder" | "children"}: compress just that folder (or each of its
        sub-folders), independent of the configured source folders and the schedule."""
        with self.lock:
            if self.running:
                return False
            self.stop_event.clear()
            self.thread = threading.Thread(target=self._run, args=(dict(cfg), trigger, dry_run, verify_only, oneoff),
                                           daemon=True)
            self.status = self._blank_status()
            self.status.update(state="running", trigger=trigger, dry_run=dry_run, started_at=time.time(),
                               mode="verify" if verify_only else "dry" if dry_run else "run")
            self._reset_run()
            self._slots = [None] * (1 if verify_only else cfg["parallel_jobs"])
            self.thread.start()
            return True

    def stop(self):
        with self.lock:
            if not self.running:
                return False
            self.stop_event.set()
            self.status["state"] = "stopping"
            for p in list(self.procs):
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
        self.log("warn", "Stop requested - finishing up")
        return True

    # ------------------------------------------------------------ paths
    @staticmethod
    def out_dir_for(root, cfg):
        if not cfg["output_dir"]:
            return root
        if cfg["output_layout"] == "mirror":
            return os.path.join(cfg["output_dir"], os.path.basename(root.rstrip(os.sep)) or "root")
        return cfg["output_dir"]

    def subfolders(self, path, cfg):
        """Names of the sub-folders of `path` that would be compressed (excluded names do not count)."""
        try:
            with os.scandir(path) as it:
                return sorted(e.name for e in it if e.is_dir(follow_symlinks=False) and not self._excluded(e.name, cfg))
        except OSError:
            return []

    def oneoff_mode(self, path, cfg):
        """A one-off folder is scanned like a source folder: each sub-folder becomes its own archive. Only a folder
        without sub-folders is compressed as a whole."""
        return "children" if self.subfolders(path, cfg) else "folder"

    def _excluded(self, name, cfg):
        if name.startswith(WORK_PREFIX) or name == mounts.RECYCLE_NAME:
            return True                                 # never the work folders or the Recycle Bin
        low = name.lower()
        return any(fnmatch.fnmatch(low, p.lower()) for p in cfg["exclude_dirs"])

    # ------------------------------------------------------------ the run
    def _run(self, cfg, trigger, dry_run, verify_only, oneoff=None):
        result, message = "completed", ""
        only = None
        label = ", verify" if verify_only else ", dry run" if dry_run else ""
        self.log("info", f"=== Run started ({trigger}{label}) ===")
        try:
            if not verify_only and not self.tools["rar"]:
                raise RuntimeError("The rar program is not installed in this container")
            sources = []
            if oneoff:
                target = os.path.realpath(oneoff["path"])
                if not os.path.isdir(target):
                    raise RuntimeError(f"Folder not found: {target}")
                mode = oneoff["mode"] if oneoff["mode"] in ("folder", "children") else self.oneoff_mode(target, cfg)
                if mode == "folder":
                    if target == os.sep:
                        raise RuntimeError("The root folder cannot be compressed as one archive")
                    sources, only = [os.path.dirname(target)], os.path.basename(target)
                    self.log("info", f"One-off: {target} has no sub-folders - compressing the folder itself")
                else:
                    sources = [target]
                    n = len(self.subfolders(target, cfg))
                    self.log("info", f"One-off: {target} has {n} sub-folder{'s' if n != 1 else ''} - compressing each one into its "
                                     "own archive (files directly inside it are left alone)")
            else:
                for s in cfg["sources"]:
                    s = os.path.realpath(s)
                    if os.path.isdir(s):
                        sources.append(s)
                    else:
                        self.log("warn", f"Source folder not found, skipping: {s}")
            if not sources:
                raise RuntimeError("No valid source folders configured")
            self._check_folders(cfg, sources)
            if not dry_run and not verify_only:
                self._prepare_folders(cfg, sources)
            if not dry_run:                          # a dry run changes nothing
                for d in self._archive_dirs(sources, cfg):
                    self._remove_stale_work(d)

            if verify_only:
                self._verify_checksums(sources, cfg)
            else:
                jobs = self._collect(sources, cfg, trigger, only)
                with self.lock:
                    self.found = len(jobs)
                self.log("info", f"Found {len(jobs)} folder(s) to compress"
                                 f" ({len(self.deferred)} changed too recently, {self.skipped} skipped)")
                if dry_run:
                    for j in jobs:
                        self._dry(j, cfg)
                else:
                    with ThreadPoolExecutor(max_workers=cfg["parallel_jobs"]) as pool:
                        results = [(j, f.result()) for j, f in [(j, pool.submit(self._work, j, cfg)) for j in jobs]]
                    for j, r in results:             # transient failures get a second try, one at a time
                        if r == "retry" and not self.stop_event.is_set():
                            self.log("info", f"Retrying on its own: {j['path']}")
                            self._work(j, cfg, True)
                if (not oneoff and cfg["checksum_existing"] and cfg["checksum_mode"] != "none"
                        and not self.stop_event.is_set()):
                    self._existing_pass(sources, cfg, dry_run)
            if self.stop_event.is_set():
                result = "stopped"
        except Exception as e:  # noqa: BLE001 - report any failure to the UI
            result, message = "error", str(e)
            self.log("error", f"Run failed: {e}")
        finally:
            self._finish(trigger, dry_run, verify_only, result, message)

    def _check_folders(self, cfg, sources):
        self._mounts = mounts.list_mounts(os.path.dirname(self.failed_path))
        if cfg["source_action"] == "recycle":
            for s in sources:
                _, why = mounts.recycle_target(os.path.join(s, "x"), self._mounts)
                if why:
                    raise RuntimeError(f"Cannot use the Recycle Bin for {s}: {why}. "
                                       "Choose another action for the original folder in Settings.")
                rb = mounts.recycle_root(os.path.join(s, "x"), self._mounts)[0]
                host = mounts.path_info(rb, self._mounts)["host"]
                self.log("info", f"Compressed folders from {s} go to the Recycle Bin: {rb}"
                                 + (f" (on the server: {host})" if host else ""))
        mv = cfg["move_dir"]
        if cfg["source_action"] == "move":
            if not mv:
                raise RuntimeError("'Move' is selected but no move folder is set")
            mv = os.path.realpath(mv)
            cfg["move_dir"] = mv
            for s in sources:
                if mv == s or mv.startswith(s + os.sep):
                    raise RuntimeError(f"Move folder {mv} is inside source {s}")
                if s.startswith(mv + os.sep):
                    raise RuntimeError(f"Source {s} is inside the move folder {mv}")
        if cfg["output_dir"]:
            out = os.path.realpath(cfg["output_dir"])
            cfg["output_dir"] = out
            for s in sources:
                if out != s and out.startswith(s + os.sep):
                    raise RuntimeError(f"Output folder {out} is inside source {s}")

    def _prepare_folders(self, cfg, sources):
        for s in sources:
            d = self.out_dir_for(s, cfg)
            if not os.path.isdir(d):
                os.makedirs(d, exist_ok=True)
                self._apply_perm(d, True, cfg)
        if cfg["source_action"] == "move":
            os.makedirs(cfg["move_dir"], exist_ok=True)
            self._apply_perm(cfg["move_dir"], True, cfg)

    def _archive_dirs(self, sources, cfg):
        """Folders that may hold archives: the sources and the output folders."""
        out = []
        for s in sources:
            for d in (s, self.out_dir_for(s, cfg)):
                if d not in out and os.path.isdir(d):
                    out.append(d)
        return out

    def _remove_stale_work(self, d):
        """Work folders left behind by a crash or a forced stop."""
        try:
            for e in os.scandir(d):
                if e.name.startswith(WORK_PREFIX) and e.is_dir(follow_symlinks=False):
                    shutil.rmtree(e.path, ignore_errors=True)
        except OSError:
            pass

    def _finish(self, trigger, dry_run, verify_only, result, message):
        with self.lock:
            st = self.status
            st["finished_at"] = time.time()
            st["state"] = "idle"
            st["result"] = result
            st["message"] = message
            failed = sum(1 for f in self.failures if not f.get("warn"))
            entry = {
                "started_at": st["started_at"], "finished_at": st["finished_at"], "trigger": trigger,
                "mode": st["mode"], "dry_run": dry_run, "result": result, "message": message,
                "found": self.found, "compressed": st["compressed"], "failed": failed,
                "deferred": len(self.deferred), "skipped": self.skipped, "crc_added": self.crc_added, "cancelled": self.cancelled,
                "files": st["files"], "bytes_in": st["bytes_in"], "bytes_out": st["bytes_out"],
                "verified": dict(self.verified) if verify_only else None,
                "failures": self.failures[-50:],
            }
            self.history.append(entry)
            self._save_history()
        if not dry_run and not verify_only:
            self.stats.run_finished()
        if verify_only:
            v = self.verified
            self.log("info", f"=== Verify {result}: {v['checked']} checked, {v['ok']} OK, {v['bad']} wrong, {v['missing']} missing ===")
        else:
            self.log("info", f"=== Run {result}: {entry['compressed']} compressed, {entry['failed']} failed"
                             f"{', ' + str(self.crc_added) + ' checksums added' if self.crc_added else ''} ===")

    # ------------------------------------------------------------ scanning
    def _inspect(self, path):
        """File count, size and the time of the newest change inside a folder.

        The change time of an item is its modification time - unless that lies in the future (a clock or timezone
        mismatch on the machine that wrote it, or a tool that stamps files with a later date). A date like that says
        nothing about when the item arrived, so the inode change time (when it was really written on this server,
        which cannot be set to a date in the future) is used instead."""
        files = size = future = 0
        err = None
        now = time.time()
        example = []                                    # first item with a future date

        def changed(st, p):
            nonlocal future
            if st.st_mtime > now + FUTURE_SLACK:
                future += 1
                if not example:
                    example.append((p, st.st_mtime))
                return st.st_ctime
            return st.st_mtime

        try:
            st0 = os.lstat(path)
            newest, newest_path = changed(st0, path), path
        except OSError:
            newest, newest_path = 0.0, path
        stack = [path]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        try:
                            st = e.stat(follow_symlinks=False)
                        except OSError as ex:
                            err = err or str(ex)
                            continue
                        t = changed(st, e.path)
                        if t > newest:
                            newest, newest_path = t, e.path
                        if stat.S_ISDIR(st.st_mode):
                            stack.append(e.path)
                        elif stat.S_ISREG(st.st_mode):
                            files += 1
                            size += st.st_size
            except OSError as ex:
                err = err or f"{d}: {ex}"
        return {"files": files, "bytes": size, "newest": newest, "error": err, "newest_path": newest_path,
                "future": future, "future_example": example[0] if example else None}

    def _existing_bases(self, out_dir):
        """Names (lower case) that already have an archive in out_dir: name.rar, name [CRC].rar, name.part1.rar."""
        if out_dir not in self._ex_cache:
            names = set()
            try:
                for fn in os.listdir(out_dir):
                    m = EXISTING_RE.match(fn)
                    if m:
                        names.add(m.group(1).lower())
                        names.add(re.sub(r"(?: \[[0-9A-Fa-f]{8}\])?\.rar$", "", fn, flags=re.I).lower())
            except OSError:
                pass
            self._ex_cache[out_dir] = names
        return self._ex_cache[out_dir]

    @staticmethod
    def _signature(job_or_info):
        return f"{job_or_info['files']}:{job_or_info['bytes']}:{int(job_or_info['newest'])}"

    def _collect(self, sources, cfg, trigger, only=None):
        jobs = []
        failed_before = self._load_failed() if trigger == "schedule" else {}
        age = cfg["min_age_minutes"]
        for root in sources:
            out_dir = self.out_dir_for(root, cfg)
            existing = self._existing_bases(out_dir)
            try:
                entries = sorted(os.scandir(root), key=lambda e: e.name)
            except OSError as e:
                self.log("warn", f"Cannot read {root}: {e}")
                continue
            for e in entries:
                if self.stop_event.is_set():
                    return jobs
                if only is not None:          # one-off on a single folder: the user picked it, so no exclusions
                    if e.name != only or not e.is_dir(follow_symlinks=False):
                        continue
                elif not e.is_dir(follow_symlinks=False) or self._excluded(e.name, cfg):
                    continue
                name = e.name
                if name.lower() in existing:
                    self.skipped += 1
                    self._warn_once(("exists", out_dir, name),
                                    f"Skipped {e.path}: an archive named '{name}' already exists in {out_dir}")
                    continue
                key = (out_dir, name.lower())
                if key in self._claimed:
                    self.skipped += 1
                    self.log("warn", f"Skipped {e.path}: another folder named '{name}' is already queued for {out_dir}")
                    continue
                info = self._inspect(e.path)
                if info["future"]:
                    p, t = info["future_example"]
                    self._warn_once(("future", e.path),
                                    f"{e.path}: {info['future']} item(s) are dated in the future (for example {os.path.relpath(p, e.path)}, "
                                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(t))}) - wrong clock or timezone on the machine that "
                                    "wrote them. Their date is ignored; the time they arrived on this server is used instead.")
                if info["files"] == 0:
                    self._warn_once(("empty", e.path), f"Skipped {e.path}: no files inside")
                    continue
                if age > 0 and time.time() - info["newest"] < age * 60:
                    self.deferred.add(e.path)
                    self.waiting[e.path] = {"ready_at": info["newest"] + age * 60, "files": info["files"], "bytes": info["bytes"],
                                            "newest": info["newest"], "newest_path": os.path.relpath(info["newest_path"], e.path),
                                            "future": info["future"]}
                    self.log("info", f"Skipped for now (changed {max(0, (time.time() - info['newest']) / 60):.0f} min ago "
                                     f"in {os.path.relpath(info['newest_path'], e.path)}, could still be copied; "
                                     f"setting: skip folders newer than {age} min): {e.path}")
                    continue
                if failed_before.get(e.path) == self._signature(info):
                    self.skipped += 1
                    self.log("info", f"Skipped {e.path}: it failed or was cancelled earlier and has not changed (press Run now to retry)")
                    continue
                self._claimed.add(key)
                jobs.append(dict(path=e.path, name=name, root=root, out_dir=out_dir, **info))
        return jobs

    def _dry(self, job, cfg):
        act = {"delete": "delete the folder", "keep": "keep the folder",
               "move": f"move the folder to {cfg['move_dir']}",
               "recycle": "move the folder to the share's .Recycle.Bin"}[cfg["source_action"]]
        tag = {"filename": " [CRC32]", "sfv": "", "none": ""}[cfg["checksum_mode"]]
        extra = " + .sfv" if cfg["checksum_mode"] == "sfv" else ""
        self.log("info", f"[dry run] would compress {job['path']} ({job['files']} files, {human(job['bytes'])}) -> "
                         f"{os.path.join(job['out_dir'], job['name'] + tag + '.rar')}{extra}, then {act}")

    # ------------------------------------------------------------ worker
    def _acquire_slot(self, path):
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl is None:
                    self._slots[i] = {"path": path, "t": time.time(), "phase": "starting", "pct": None, "cancel": False}
                    return i
        return 0

    def _release_slot(self, i):
        with self.lock:
            self._slots[i] = None

    def _halted(self, slot):
        """True when the whole run was stopped or this one job was cancelled."""
        if self.stop_event.is_set():
            return True
        sl = self._slots[slot] if slot is not None and slot < len(self._slots) else None
        return bool(sl and sl.get("cancel"))

    def cancel_job(self, path):
        """Cancel the compression of one folder while the others carry on. Nothing is changed: the unfinished
        archive is deleted and the folder stays as it is. Possible until the job starts finishing."""
        with self.lock:
            for i, sl in enumerate(self._slots):
                if sl and sl["path"] == path:
                    if sl["phase"] == "finishing":
                        return False, "It is already finishing - too late to cancel"
                    sl["cancel"] = True
                    for p in list(self._slot_procs.get(i, ())):
                        try:
                            os.killpg(p.pid, signal.SIGTERM)
                        except OSError:
                            pass
                    break
            else:
                return False, "That folder is not being compressed any more"
        self.log("warn", f"Cancel requested: {path}")
        return True, ""

    def _phase(self, slot, phase, pct=None):
        with self.lock:
            if slot < len(self._slots) and self._slots[slot]:
                self._slots[slot]["phase"] = phase
                self._slots[slot]["pct"] = pct

    def _enter_finishing(self, slot):
        """Switch to "finishing" (after this the job can no longer be cancelled) - unless it was stopped or cancelled
        just before: checked under the same lock cancel_job uses, so a cancel is either refused or honoured."""
        with self.lock:
            if self._halted(slot):
                raise Failed("stopped")
            if slot < len(self._slots) and self._slots[slot]:
                self._slots[slot]["phase"] = "finishing"
                self._slots[slot]["pct"] = None

    def _work(self, job, cfg, final=False):
        """Returns "ok", "retry" (failed in a way that may pass when tried again on its own) or "fail"."""
        if self.stop_event.is_set():
            return "fail"
        slot = self._acquire_slot(job["path"])
        try:
            self.log("info", f"[job {slot + 1}] Compressing {job['path']} ({job['files']} files, {human(job['bytes'])})")
            self._compress(job, cfg, slot)
            self._update_failed(job["path"])
            return "ok"
        except Failed as e:
            if self.stop_event.is_set():
                self.log("warn", f"Stopped: {job['path']}")
                return "fail"
            if self._halted(slot):                           # cancelled by the user
                with self.lock:
                    self.cancelled += 1
                self._update_failed(job["path"], self._signature(job))   # scheduled scans leave it alone until it changes
                self.log("warn", f"Cancelled: {job['path']} - nothing was changed. Scheduled scans skip it until it "
                                 "changes; Run now (or Compress folder...) tries it again")
                return "fail"
            if e.transient and not final:
                self.log("warn", f"Failed, will retry on its own after the other folders: {job['path']} - {e}")
                return "retry"
            self._fail(job["path"], str(e))
            if e.remember:
                self._update_failed(job["path"], self._signature(job))
            return "fail"
        except Exception as e:  # noqa: BLE001 - one folder must never stop the others
            self._fail(job["path"], f"unexpected error: {e}")
            return "fail"
        finally:
            self._release_slot(slot)

    def _fail(self, path, err, warn=False):
        with self.lock:
            self.failures.append({"path": path, "error": err, "warn": warn})
        if not warn:
            self.stats.failed()
        self.log("error" if not warn else "warn", f"{'PROBLEM' if warn else 'FAILED (left in place)'}: {path} - {err}")

    # ------------------------------------------------------------ one folder
    def _rar_command(self, cfg, archive, target):
        rar = self.tools["rar"]
        c = [rar, "a", f"-m{cfg['level']}", "-ma5", "-ol", "-r", "-idc", "-y", "-s" if cfg["solid"] else "-s-"]
        if cfg["recovery_percent"]:
            c.append(f"-rr{cfg['recovery_percent']}%")
        if cfg["dictionary"] != "auto":
            c.append(f"-md{cfg['dictionary']}")
        if cfg["rar_threads"]:
            c.append(f"-mt{cfg['rar_threads']}")
        if cfg["volume_mb"]:
            c.append(f"-v{cfg['volume_mb'] * 1048576}b")
        if cfg["password"]:
            c.append(("-hp" if cfg["encrypt_names"] else "-p") + cfg["password"])
        for pat in cfg["exclude_files"]:
            c.append("-x" + pat)
            if "/" not in pat:
                c.append("-x*/" + pat)     # a bare name only matches at the top level, so match in sub-folders too
        return c + ["--", archive, target]

    def _exec(self, cmd, cwd, slot=None, phase=""):
        """Run a command in its own process group (so Stop can kill it) and follow rar's percent output."""
        if slot is not None and self._halted(slot):
            return None, "cancelled"
        try:
            p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        except OSError as e:
            return None, str(e)
        with self.lock:
            self.procs.add(p)
            if slot is not None:
                self._slot_procs.setdefault(slot, set()).add(p)
        if slot is not None and self._halted(slot):       # cancelled between the check and the start
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        tail = b""
        try:
            while True:
                chunk = p.stdout.read1(4096)
                if not chunk:
                    break
                tail = (tail + chunk)[-3000:]
                if slot is not None:
                    pcts = re.findall(rb"(\d{1,3})%", chunk)
                    if pcts:
                        self._phase(slot, phase, min(100, int(pcts[-1])))
            p.wait()
        finally:
            with self.lock:
                self.procs.discard(p)
                if slot is not None:
                    self._slot_procs.get(slot, set()).discard(p)
        return p.returncode, tail.decode("utf-8", "replace")

    @staticmethod
    def _transient(code, out):
        """Out of memory, write error or no space: can pass when the job runs alone."""
        return code in TRANSIENT_CODES or "no space left" in (out or "").lower()

    @staticmethod
    def _rar_error(code, out):
        text = re.sub(r"[\x08\r]+", "\n", out)
        lines = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not re.fullmatch(r"[\d%\s.]*(OK|Done)?", ln.strip())]
        tail = " | ".join(lines[-3:])[-300:]
        why = RAR_CODES.get(code, "") if code is not None else ""
        return f"rar exit code {code}" + (f" ({why})" if why else "") + (f": {tail}" if tail else "")

    def _count_files(self, rar, vols, pw, cwd, slot=None):
        """Number of files stored in the archive: `rar lt` lists one volume at a time, and a file that continues
        in the next volume is listed in both, so names are collected in a set."""
        names = set()
        for vol in vols:
            cmd = [rar, "lt", "-idc", "-y"] + ([f"-hp{pw}"] if pw else []) + ["--", vol]
            p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
            with self.lock:
                self.procs.add(p)
                if slot is not None:
                    self._slot_procs.setdefault(slot, set()).add(p)
            if slot is not None and self._halted(slot):     # cancelled between the check and the start
                try:
                    os.killpg(p.pid, signal.SIGTERM)
                except OSError:
                    pass
            try:
                last = None
                for ln in p.stdout:
                    t = ln.strip()
                    if t.startswith("Name: "):
                        last = ln.split("Name: ", 1)[1].rstrip("\n")
                    elif t == "Type: File" and last is not None:
                        names.add(last)
                p.wait()
            finally:
                with self.lock:
                    self.procs.discard(p)
                    if slot is not None:
                        self._slot_procs.get(slot, set()).discard(p)
            if slot is not None and self._halted(slot):
                raise Failed("stopped")
            if p.returncode != 0:
                raise Failed(f"listing the archive failed (rar exit code {p.returncode})", True)
        return len(names)

    def _compress(self, job, cfg, slot):
        name, path, out_dir = job["name"], job["path"], job["out_dir"]
        rar, pw = self.tools["rar"], cfg["password"]
        if re.search(r"[*?]", name) or name.startswith("@"):
            raise Failed("the folder name contains * or ? or starts with @, which RAR treats as special - rename the folder")
        if len((name + " [XXXXXXXX].part100.rar").encode()) > 255:
            raise Failed("the folder name is too long to build an archive name from it")
        need = int(job["bytes"] * (1 + cfg["recovery_percent"] / 100))
        try:
            free = shutil.disk_usage(out_dir).free
        except OSError as e:
            raise Failed(f"cannot use the output folder {out_dir}: {e}")
        if free < need:
            raise Failed(f"not enough free space in {out_dir} (up to {human(need)} needed, {human(free)} free)",
                         transient=True)       # other jobs' work files may be using it right now
        try:
            work = tempfile.mkdtemp(prefix=WORK_PREFIX, dir=out_dir)
        except OSError as e:
            raise Failed(f"cannot create a work folder in {out_dir}: {e}")
        t0 = time.time()
        try:
            # ---- 1. build the archive in the work folder
            self._phase(slot, "compressing", 0)
            archive = os.path.join(work, name + ".rar")
            if cfg["include_root"]:
                cwd, target = job["root"], name
            else:
                cwd, target = path, "*"
            code, out = self._exec(self._rar_command(cfg, archive, target), cwd, slot, "compressing")
            if self._halted(slot):
                raise Failed("stopped")
            if code != 0:
                raise Failed(self._rar_error(code, out) + (f" (some files could not be read: {job['error']})" if job["error"] else ""),
                             True, self._transient(code, out))
            vols = sorted((f for f in os.listdir(work) if f.lower().endswith(".rar")),
                          key=lambda f: (int(PART_RE.match(f).group(2)) if PART_RE.match(f) else 0, f))
            if not vols:
                raise Failed("rar finished but produced no archive", True)
            first = vols[0]
            # ---- 2. test it and compare with the folder
            if cfg["verify"]:
                self._phase(slot, "testing", 0)
                code, out = self._exec([rar, "t", "-idc", "-y"] + ([f"-hp{pw}"] if pw else []) + ["--", first],
                                       work, slot, "testing")
                if self._halted(slot):
                    raise Failed("stopped")
                if code != 0:
                    raise Failed("the finished archive failed its test - " + self._rar_error(code, out), True,
                                 self._transient(code, out))
                if not cfg["exclude_files"]:
                    self._phase(slot, "comparing")
                    n = self._count_files(rar, vols, pw, work, slot)
                    if self._halted(slot):
                        raise Failed("stopped")
                    if n != job["files"]:
                        raise Failed(f"the archive holds {n} files but the folder has {job['files']} - not removing anything", True)
            # ---- 3. checksum
            mode = cfg["checksum_mode"]
            if mode == "filename" and len(vols) > 1:
                self.log("info", f"{name}: split archives cannot carry the CRC in their names - writing an SFV file instead")
                mode = "sfv"
            crcs = {}
            if mode != "none":
                self._phase(slot, "checksum")
                for f in vols:
                    crcs[f] = crc32_file(os.path.join(work, f), lambda: self._halted(slot))
            placed = {}                                    # work file -> final name
            for f in vols:
                placed[f] = f"{name} [{crcs[f]}].rar" if mode == "filename" else f
            if mode == "sfv":
                sfv = os.path.join(work, name + ".sfv")
                with open(sfv, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(f"; Generated by AutoCompress build {os.environ.get('BUILD_NUMBER') or 'dev'} "
                             f"on {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
                    for f in vols:
                        fh.write(f"{f} {crcs[f]}\n")
                placed[name + ".sfv"] = name + ".sfv"
            # ---- 4. did the folder change meanwhile?
            self._enter_finishing(slot)
            after = self._inspect(path)
            if self._signature(after) != self._signature(job):
                raise Failed("the folder changed while it was being compressed - the archive was discarded and "
                             "the folder is untouched; it will be tried again on the next scan")
            for f, final in placed.items():
                dst = os.path.join(out_dir, final)
                if os.path.lexists(dst) and not final.endswith(".sfv"):
                    raise Failed(f"{final} already exists in {out_dir} - not overwriting it")
            # ---- 5. move into place
            for f, final in placed.items():
                dst = os.path.join(out_dir, final)
                os.replace(os.path.join(work, f), dst)
                self._apply_perm(dst, False, cfg)
            self._ex_cache.get(out_dir, set()).add(name.lower())
        finally:
            shutil.rmtree(work, ignore_errors=True)

        secs = time.time() - t0
        bytes_out = sum(os.path.getsize(os.path.join(out_dir, final)) for f, final in placed.items() if final.endswith(".rar"))
        final_names = [v for k, v in placed.items() if v.endswith(".rar")]
        # ---- 6. the folder itself
        action, problem, where, shown = cfg["source_action"], None, None, None
        try:
            if action == "delete":
                shutil.rmtree(path)
            elif action == "move":
                self._move_dir(path, job["root"], cfg)
            elif action == "recycle":
                where = self._recycle_dir(path, cfg)
        except (OSError, RuntimeError) as e:
            what, left = "removed" if action == "delete" else "moved", None
            if action == "delete":
                left = self._inspect(path)["files"] if os.path.isdir(path) else 0
            if left is not None and left < job["files"]:      # rmtree got part of the way
                problem = (f"the archive was created and tested, but the folder could only be partly removed "
                           f"({job['files'] - left} of {job['files']} files deleted, {left} left): {e}")
                shown = "partly removed"
            else:
                problem, shown = f"the archive was created and tested, but the folder could not be {what}: {e}", None
            action = "keep"
        with self.lock:
            self.status["compressed"] += 1
            self.status["files"] += job["files"]
            self.status["bytes_in"] += job["bytes"]
            self.status["bytes_out"] += bytes_out
        self.stats.archive(path, job["files"], job["bytes"], bytes_out, secs, cfg["level"], action, mode, len(vols))
        ratio = f", {100 * (1 - bytes_out / job['bytes']):.0f}% smaller" if job["bytes"] and bytes_out < job["bytes"] else ""
        self.log("info", f"Done: {name} -> {final_names[0]}{' (+' + str(len(vols) - 1) + ' volumes)' if len(vols) > 1 else ''}"
                         f" - {human(job['bytes'])} -> {human(bytes_out)}{ratio} in {secs:.0f}s"
                         f"{', folder ' + (shown or {'delete': 'deleted', 'move': 'moved', 'recycle': 'moved to the Recycle Bin: ' + str(where)}.get(action, 'kept'))}")
        if problem:
            self._fail(path, problem, warn=True)

    def _recycle_dir(self, src, cfg):
        """Move into <share>/.Recycle.Bin/<path inside the share>, where the Unraid Recycle Bin plugin keeps
        files deleted over SMB (so it is listed and emptied by the plugin)."""
        parent, why = mounts.recycle_target(src, self._mounts)
        if why:
            raise RuntimeError(f"Recycle Bin not usable: {why}")
        dest = os.path.join(parent, os.path.basename(src))
        with self.move_lock:
            missing, p = [], parent
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for m in reversed(missing):
                os.mkdir(m)
                self._apply_perm(m, True, cfg)
            if os.path.exists(dest):                 # never overwrite something already in the bin
                n = 1
                while os.path.exists(f"{dest}.{n}"):
                    n += 1
                dest = f"{dest}.{n}"
            shutil.move(src, dest)
        return dest

    def _move_dir(self, src, root, cfg):
        """Move into move_dir/<source folder name>/<folder name>."""
        base = os.path.basename(root.rstrip(os.sep)) or "root"
        dest = os.path.join(cfg["move_dir"], base, os.path.basename(src))
        with self.move_lock:
            missing, p = [], os.path.dirname(dest)
            while not os.path.isdir(p):
                missing.append(p)
                p = os.path.dirname(p)
            for m in reversed(missing):
                os.mkdir(m)
                self._apply_perm(m, True, cfg)
            if os.path.exists(dest):                 # never overwrite something already moved
                n = 1
                while os.path.exists(f"{dest}.{n}"):
                    n += 1
                dest = f"{dest}.{n}"
            shutil.move(src, dest)

    # ------------------------------------------------------------ existing archives
    @staticmethod
    def _group_archives(d):
        """Archives in a folder: [(base, kind, [files])], kind = single | parts | old. Hidden files are skipped."""
        try:
            names = sorted(n for n in os.listdir(d) if not n.startswith(".") and os.path.isfile(os.path.join(d, n)))
        except OSError:
            return []
        lower = {n.lower() for n in names}
        sets, singles = {}, []
        for n in names:
            m = PART_RE.match(n)
            if m:
                sets.setdefault((m.group(1), "parts"), []).append(n)
            elif n.lower().endswith(".rar"):
                base = n[:-4]
                if base.lower() + ".r00" in lower:
                    sets[(base, "old")] = [n] + [x for x in names if re.fullmatch(re.escape(base) + r"\.r\d\d", x, re.I)]
                else:
                    singles.append((base, "single", [n]))
        return singles + [(b, k, f) for (b, k), f in sorted(sets.items())]

    def _existing_pass(self, sources, cfg, dry):
        mode = cfg["checksum_mode"]
        for d in self._archive_dirs(sources, cfg):
            for base, kind, files in self._group_archives(d):
                if self.stop_event.is_set():
                    return
                if mode == "filename":
                    if kind != "single":
                        self._warn_once(("set", d, base), f"Not renaming {base} ({kind} set): renaming one part would break the set. "
                                                          "Use the SFV option for split archives.")
                        continue
                    if CRC_RE.search(base):
                        continue
                    src = os.path.join(d, files[0])
                    if dry:
                        self.log("info", f"[dry run] would add the CRC to {src}")
                        continue
                    try:
                        crc = crc32_file(src, self.stop_event.is_set)
                        dst = os.path.join(d, f"{base} [{crc}].rar")
                        if os.path.lexists(dst):
                            self._warn_once(("clash", dst), f"Not renaming {src}: {dst} already exists")
                            continue
                        os.rename(src, dst)
                    except Failed:
                        return
                    except OSError as e:
                        self._fail(src, f"could not add the CRC: {e}", warn=True)
                        continue
                    self.log("info", f"CRC added: {files[0]} -> {os.path.basename(dst)}")
                else:  # sfv
                    sfv_base = re.sub(r" \[[0-9A-Fa-f]{8}\]$", "", base) if kind == "single" else base
                    sfv = os.path.join(d, sfv_base + ".sfv")
                    if os.path.exists(sfv) or os.path.exists(os.path.join(d, base + ".sfv")):
                        continue
                    if dry:
                        self.log("info", f"[dry run] would write {sfv}")
                        continue
                    try:
                        lines = [f"{f} {crc32_file(os.path.join(d, f), self.stop_event.is_set)}" for f in files]
                        with open(sfv, "w", encoding="utf-8", newline="\n") as fh:
                            fh.write(f"; Generated by AutoCompress build {os.environ.get('BUILD_NUMBER') or 'dev'} "
                                     f"on {time.strftime('%Y-%m-%d %H:%M:%S')}\n" + "\n".join(lines) + "\n")
                        self._apply_perm(sfv, False, cfg)
                    except Failed:
                        return
                    except OSError as e:
                        self._fail(sfv, f"could not write the SFV file: {e}", warn=True)
                        continue
                    self.log("info", f"SFV written: {os.path.basename(sfv)}")
                with self.lock:
                    self.crc_added += 1
                self.stats.checksum(mode)

    # ------------------------------------------------------------ verify checksums (on demand)
    def _verify_checksums(self, sources, cfg):
        """Recompute the CRC of every archive that has one in its name or in an SFV file and compare."""
        def report(path, expected, actual):
            with self.lock:
                self.verified["checked"] += 1
            if actual is None:
                with self.lock:
                    self.verified["missing"] += 1
                self._fail(path, "file is missing", warn=False)
            elif actual != expected:
                with self.lock:
                    self.verified["bad"] += 1
                self._fail(path, f"checksum is {actual} but {expected} was recorded - the file changed or is damaged")
            else:
                with self.lock:
                    self.verified["ok"] += 1
                self.log("info", f"OK  {os.path.basename(path)}  {actual}")

        for d in self._archive_dirs(sources, cfg):
            try:
                names = sorted(n for n in os.listdir(d) if not n.startswith(".") and os.path.isfile(os.path.join(d, n)))
            except OSError:
                continue
            for n in names:
                if self.stop_event.is_set():
                    return
                full = os.path.join(d, n)
                try:
                    if n.lower().endswith(".rar") and CRC_RE.search(n[:-4]):
                        report(full, CRC_RE.search(n[:-4]).group(1).upper(), crc32_file(full, self.stop_event.is_set))
                    elif n.lower().endswith(".sfv"):
                        for fn, crc in parse_sfv(full):
                            p = os.path.join(d, fn)
                            report(p, crc, crc32_file(p, self.stop_event.is_set) if os.path.isfile(p) else None)
                except Failed:
                    return
                except OSError as e:
                    self._fail(full, f"could not be read: {e}")

    # ------------------------------------------------------------ permissions
    def _apply_perm(self, path, is_dir, cfg):
        try:
            if cfg["set_perms"]:
                os.chmod(path, int(cfg["dir_mode" if is_dir else "file_mode"], 8))
            if cfg["chown_enabled"]:
                os.chown(path, cfg["uid"], cfg["gid"])
        except OSError as e:
            self._warn_once(("perm", type(e).__name__), f"Permission change failed ({path}): {e}")
