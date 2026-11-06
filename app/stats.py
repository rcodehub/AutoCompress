"""Lifetime statistics, kept in /data/stats.json (survive restarts and updates)."""
import json
import os
import tempfile
import threading
import time

TOTAL_KEYS = ("archives", "files", "bytes_in", "bytes_out", "freed", "moved", "recycled", "kept", "failed", "runs",
              "seconds", "crc_names", "sfv", "volumes")
DAY_KEYS = ("archives", "files", "bytes_in", "bytes_out", "failed")
MAX_DAYS = 400


def blank():
    return {"since": time.time(), "totals": {k: 0 for k in TOTAL_KEYS}, "levels": {}, "days": {}, "records": {}}


class Stats:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.save_lock = threading.Lock()      # one writer at a time; never held together with self.lock
        self.data = self._load()
        self._dirty = 0

    def _load(self):
        b = blank()
        try:
            with open(self.path) as f:
                d = json.load(f)
        except (OSError, ValueError):
            return b
        if not isinstance(d, dict):
            return b
        for k, default in b.items():                 # a key of the wrong type is replaced, the rest is kept
            want = (int, float) if k == "since" else type(default)
            if not isinstance(d.get(k), want) or isinstance(d.get(k), bool):
                d[k] = default
        for k in TOTAL_KEYS:
            if not isinstance(d["totals"].get(k), (int, float)) or isinstance(d["totals"].get(k), bool):
                d["totals"][k] = 0
        for key in ("levels", "days", "records"):    # entries that are not objects would break the counters later
            d[key] = {k: v for k, v in d[key].items() if isinstance(v, dict)}
        for k, day in d["days"].items():
            for dk in DAY_KEYS:
                if not isinstance(day.get(dk), (int, float)) or isinstance(day.get(dk), bool):
                    day[dk] = 0
        for k, lv in d["levels"].items():
            for lk in ("n", "in", "out"):
                if not isinstance(lv.get(lk), (int, float)) or isinstance(lv.get(lk), bool):
                    lv[lk] = 0
        d["records"] = {k: v for k, v in d["records"].items()
                        if isinstance(v.get("value"), (int, float)) and not isinstance(v.get("value"), bool)}
        return d

    def save(self):
        with self.lock:
            snap = json.dumps(self.data)
            self._dirty = 0
        with self.save_lock:                         # a unique temp file, and only one writer at a time
            tmp = None
            try:
                fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or ".", prefix=".stats-", suffix=".tmp")
                with os.fdopen(fd, "w") as f:
                    f.write(snap)
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.path)
                tmp = None
            except OSError:
                pass
            finally:
                if tmp:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass

    def _day(self):
        days = self.data["days"]
        key = time.strftime("%Y-%m-%d")
        if key not in days:
            days[key] = {k: 0 for k in DAY_KEYS}
            for old in sorted(days)[:-MAX_DAYS]:
                del days[old]
        return days[key]

    def _record(self, name, value, path, unit):
        r = self.data["records"].get(name)
        if not r or value > r["value"]:
            self.data["records"][name] = {"value": value, "path": path, "unit": unit, "at": time.time()}

    def archive(self, path, files, bytes_in, bytes_out, seconds, level, action, checksum, volumes=1):
        """One folder compressed. action: what happened to the folder (delete | move | keep)."""
        with self.lock:
            tot, day = self.data["totals"], self._day()
            for d in (tot, day):
                d["archives"] += 1
                d["files"] += files
                d["bytes_in"] += bytes_in
                d["bytes_out"] += bytes_out
            tot["seconds"] += seconds
            tot[{"delete": "freed", "move": "moved", "recycle": "recycled"}.get(action, "kept")] += bytes_in
            tot["volumes"] += volumes if volumes > 1 else 0
            if checksum == "filename":
                tot["crc_names"] += 1
            elif checksum == "sfv":
                tot["sfv"] += 1
            lv = self.data["levels"].setdefault(str(level), {"n": 0, "in": 0, "out": 0})
            lv["n"] += 1
            lv["in"] += bytes_in
            lv["out"] += bytes_out
            self._record("largest", bytes_in, path, "bytes")
            self._record("most_files", files, path, "files")
            if bytes_in >= 1_000_000 and bytes_out < bytes_in:
                self._record("best_ratio", 1 - bytes_out / bytes_in, path, "ratio")
            if seconds >= 1 and bytes_in >= 50_000_000:
                self._record("fastest", bytes_in / seconds, path, "bytes/s")
            self._dirty += 1
            due = self._dirty >= 5
        if due:
            self.save()

    def checksum(self, kind):
        """A checksum was added to an archive that already existed."""
        with self.lock:
            self.data["totals"]["crc_names" if kind == "filename" else "sfv"] += 1
            self._dirty += 1

    def failed(self):
        with self.lock:
            self.data["totals"]["failed"] += 1
            self._day()["failed"] += 1

    def run_finished(self):
        with self.lock:
            self.data["totals"]["runs"] += 1
        self.save()

    def snapshot(self):
        with self.lock:
            return json.loads(json.dumps(self.data))

    def reset(self):
        with self.lock:
            self.data = blank()
        self.save()
