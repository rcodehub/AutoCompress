"""Lifetime statistics, kept in /data/stats.json (survive restarts and updates)."""
import json
import os
import threading
import time

TOTAL_KEYS = ("archives", "files", "bytes_in", "bytes_out", "freed", "moved", "kept", "failed", "runs",
              "seconds", "crc_names", "sfv", "volumes")
DAY_KEYS = ("archives", "files", "bytes_in", "bytes_out", "failed")
MAX_DAYS = 400


def blank():
    return {"since": time.time(), "totals": {k: 0 for k in TOTAL_KEYS}, "levels": {}, "days": {}, "records": {}}


class Stats:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = self._load()
        self._dirty = 0

    def _load(self):
        try:
            with open(self.path) as f:
                d = json.load(f)
            b = blank()
            for k in b:
                d.setdefault(k, b[k])
            for k in TOTAL_KEYS:
                d["totals"].setdefault(k, 0)
            return d
        except (OSError, ValueError, AttributeError):
            return blank()

    def save(self):
        with self.lock:
            snap = json.dumps(self.data)
            self._dirty = 0
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                f.write(snap)
            os.replace(tmp, self.path)
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
            tot[{"delete": "freed", "move": "moved"}.get(action, "kept")] += bytes_in
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
