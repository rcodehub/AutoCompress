"""Settings: defaults, first-start values from the mapped folders, validation, storage."""
import json
import os
import re
import threading

import mounts

# App data: settings.json, history, stats, logs and the login key. Mapped to e.g. /mnt/user/appdata/autocompress.
CONFIG_DIR = os.environ.get("DATA_DIR") or "/data"
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

LEVELS = {0: "Store (no compression)", 1: "Fastest", 2: "Fast", 3: "Normal", 4: "Good", 5: "Best"}
DICTIONARIES = ("auto", "32m", "64m", "128m", "256m", "512m", "1g")
CHECKSUM_MODES = ("filename", "sfv", "none")
SOURCE_ACTIONS = ("recycle", "delete", "move", "keep")
LAYOUTS = ("flat", "mirror")

DEFAULTS = {
    "sources": [],                    # folders whose sub-folders are compressed (paths as seen inside the container)
    "schedule_enabled": False,        # scan on an interval
    "interval_minutes": 60,           # minutes between the end of one scan and the start of the next
    "run_on_start": False,            # also scan right after the container starts
    "parallel_jobs": 1,               # folders compressed at the same time
    "min_age_minutes": 10,            # leave folders that changed more recently than this (still being copied)
    "exclude_dirs": ["@eaDir", ".Recycle.Bin", "#recycle", ".Trash*"],   # sub-folder names that are never compressed

    "output_dir": "",                 # where the .rar files go ("" = next to the folder, in the source folder)
    "output_layout": "flat",          # flat: <output>/<name>.rar | mirror: <output>/<source folder name>/<name>.rar

    "level": 5,                       # rar -m0 .. -m5 (5 = best)
    "solid": True,                    # solid archive (better ratio, slower to extract single files)
    "dictionary": "auto",             # rar -md
    "recovery_percent": 15,           # rar -rr (0 = none)
    "rar_threads": 0,                 # rar -mt (0 = rar decides, normally all cores)
    "volume_mb": 0,                   # split into volumes of this many MB (0 = one file)
    "include_root": True,             # the archive contains the folder itself (name/...) instead of only its contents
    "exclude_files": [],              # file/folder names left out of the archive, e.g. Thumbs.db, *.tmp
    "password": "",                   # encrypt the archive (kept in settings.json, never sent back to the browser)
    "encrypt_names": True,            # with a password: also encrypt the file names (rar -hp)
    "verify": True,                   # test the archive and compare the file count before touching the folder

    "checksum_mode": "filename",      # filename: "name [CRC32].rar" | sfv: name.sfv next to the archive | none
    "checksum_existing": False,       # also add the checksum to .rar files that are already there

    "source_action": "recycle",       # after it was compressed and verified: recycle (share's .Recycle.Bin) | delete | move | keep
    "move_dir": "",

    "set_perms": True,                # chmod the archives that are created
    "file_mode": "666",
    "dir_mode": "777",
    "chown_enabled": False,           # also set the owner
    "uid": 99,                        # Unraid: nobody
    "gid": 100,                       # Unraid: users
}

_lock = threading.Lock()


def env_defaults():
    """First-start settings, from the folders mapped in the template: /source is scanned, /output (if mapped)
    receives the archives and /trash (if mapped) receives compressed folders instead of deleting them."""
    d = dict(DEFAULTS)
    d["sources"] = mounts.suggested_sources(CONFIG_DIR)
    out = mounts.suggested_output_dir()
    if out:
        d["output_dir"] = out
    move = mounts.suggested_move_dir()
    if move:
        d.update(move_dir=move, source_action="move")
    else:
        # default: the share's Recycle Bin - but only when it can be reached; otherwise keep the folders (never delete by surprise)
        mnt = mounts.list_mounts(CONFIG_DIR)
        if any(mounts.recycle_target(os.path.join(p, "x"), mnt)[1] for p in d["sources"]) or not d["sources"]:
            d["source_action"] = "keep"
    return d


def _lines(value):
    """Accept a list or a newline/comma separated string; return clean unique entries."""
    if isinstance(value, str):
        value = re.split(r"[\n,]", value)
    out = []
    for x in value or []:
        x = str(x).strip()
        if x and x not in out:
            out.append(x)
    return out


def _within(path, parent):
    path, parent = os.path.normpath(path), os.path.normpath(parent)
    return path == parent or path.startswith(parent.rstrip(os.sep) + os.sep)


def mapped_roots():
    """Folders mapped into the container (the app data folder is not one of them)."""
    return mounts.browse_roots(CONFIG_DIR)


def validate(data, roots=None):
    """Return (clean_settings, errors). Unknown keys are dropped, missing ones get defaults.
    roots: when given, source/move/output folders have to be inside one of these (the mapped folders)."""
    s, errors, _ = check(data, roots)
    return s, errors


def check(data, roots=None):
    """validate() plus the names of the path settings that failed; those are put back to their default
    in the returned settings (a save is refused anyway, a lenient load keeps everything else)."""
    errors = []
    bad = set()
    s = dict(DEFAULTS)

    def num(key, lo, hi, label):
        try:
            v = int(data.get(key, s[key]))
        except (TypeError, ValueError):
            errors.append(f"{label} must be a whole number")
            return
        if not lo <= v <= hi:
            errors.append(f"{label} must be between {lo} and {hi}")
            return
        s[key] = v

    def boolean(key):
        v = data.get(key, s[key])
        s[key] = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes", "on")

    def choice(key, label, options):
        v = str(data.get(key, s[key])).lower()
        if v not in options:
            errors.append(f"{label} must be one of: {', '.join(options)}")
        else:
            s[key] = v

    s["sources"] = [os.path.normpath(p) for p in _lines(data.get("sources", s["sources"]))]
    bad_sources = set()
    for p in s["sources"]:
        if not p.startswith("/"):
            errors.append(f"Source folder must be an absolute path: {p}")
            bad_sources.add(p)
        elif _within(p, CONFIG_DIR):
            errors.append(f"Source folder {p} is the app data folder - choose a folder such as /source")
            bad_sources.add(p)
        elif roots is not None and not mounts.inside_roots(p, roots):
            errors.append(f"Source folder {p} is not inside a folder mapped into the container")
            bad_sources.add(p)
    # only the invalid source folders are dropped; the valid ones stay (a save is refused anyway)
    s["sources"] = [p for p in s["sources"] if p not in bad_sources]

    for k in ("schedule_enabled", "run_on_start", "solid", "include_root", "encrypt_names", "verify",
              "checksum_existing", "set_perms", "chown_enabled"):
        boolean(k)
    num("interval_minutes", 1, 525600, "Scan interval")
    num("parallel_jobs", 1, 16, "Folders at once")
    num("min_age_minutes", 0, 100000, "Minimum folder age")
    num("level", 0, 5, "Compression level")
    num("recovery_percent", 0, 100, "Recovery record")
    num("rar_threads", 0, 64, "RAR threads")
    num("volume_mb", 0, 100_000_000, "Volume size")
    num("uid", 0, 4294967295, "User ID")
    num("gid", 0, 4294967295, "Group ID")
    choice("dictionary", "Dictionary size", DICTIONARIES)
    choice("checksum_mode", "Checksum", CHECKSUM_MODES)
    choice("source_action", "Folder action", SOURCE_ACTIONS)
    choice("output_layout", "Output layout", LAYOUTS)

    s["exclude_dirs"] = _lines(data.get("exclude_dirs", s["exclude_dirs"]))
    s["exclude_files"] = _lines(data.get("exclude_files", s["exclude_files"]))
    for pat in s["exclude_files"]:
        if re.search(r"[\x00-\x1f]", pat):
            errors.append(f"Exclude pattern contains control characters: {pat!r}")

    pw = data.get("password", s["password"])
    pw = "" if pw is None else str(pw)
    if len(pw) > 127 or re.search(r"[\x00-\x1f]", pw):
        errors.append("Password must be at most 127 characters, without control characters")
    else:
        s["password"] = pw

    for k, label in (("file_mode", "File permissions"), ("dir_mode", "Folder permissions")):
        v = str(data.get(k, s[k])).strip()
        if not re.fullmatch(r"[0-7]{3,4}", v):
            errors.append(f"{label} must be an octal mode such as 666 or 777")
        else:
            s[k] = v

    def folder(key, label):
        """Checks every destination folder has to pass: absolute, away from the app data folder, mapped."""
        s[key] = str(data.get(key, "") or "").strip()
        if not s[key]:
            return False
        s[key] = os.path.normpath(s[key])
        if not s[key].startswith("/"):
            errors.append(f"{label} must be an absolute path")
        elif _within(s[key], CONFIG_DIR) or _within(CONFIG_DIR, s[key]):
            errors.append(f"{label} {s[key]} must not be, contain or be inside the app data folder {CONFIG_DIR}")
        elif roots is not None and not mounts.inside_roots(s[key], roots):
            errors.append(f"{label} {s[key]} is not inside a folder mapped into the container")
        else:
            return True
        bad.add(key)
        return False

    if folder("output_dir", "Output folder"):
        for src in s["sources"]:
            if _within(s["output_dir"], src) and s["output_dir"] != src:
                errors.append(f"Output folder {s['output_dir']} must not be inside the source folder {src} - "
                              "it would be compressed itself. Use a separate mapping.")
                bad.add("output_dir")
            elif _within(src, s["output_dir"]) and s["output_dir"] != src:
                errors.append(f"Source folder {src} must not be inside the output folder {s['output_dir']} - "
                              "give the source folder and the output folder separate mappings")
                bad.add("output_dir")

    if folder("move_dir", "Move folder"):
        for src in s["sources"]:
            if _within(s["move_dir"], src):
                errors.append(f"Move folder {s['move_dir']} must not be inside the source folder {src}")
                bad.add("move_dir")
            elif _within(src, s["move_dir"]):
                errors.append(f"Source folder {src} must not be inside the move folder {s['move_dir']} - "
                              "give the source folder and the move folder separate mappings")
                bad.add("move_dir")
    if s["source_action"] == "move" and not s["move_dir"]:
        errors.append("A move folder is required when the folder action is set to move")
        bad.add("source_action")
    elif s["source_action"] == "move" and "move_dir" in bad:
        bad.add("source_action")             # the move folder was refused: do not keep "move" without a folder

    for k in bad:
        s[k] = list(DEFAULTS[k]) if isinstance(DEFAULTS[k], list) else DEFAULTS[k]
    return s, errors, bad


def load_with_report():
    """Return (settings, problems, source). One bad value never throws the others away:
    every valid saved setting is kept, only the invalid ones fall back to their default.
    source: "file" (saved settings), "defaults" (nothing saved yet) or "unreadable"."""
    with _lock:
        data, source = None, "defaults"
        try:
            with open(SETTINGS_PATH) as f:
                data = json.load(f)
            source = "file"
            if not isinstance(data, dict):
                data, source = None, "unreadable"
        except FileNotFoundError:
            pass
        except (OSError, ValueError):
            source = "unreadable"
        if data is None:                      # nothing usable saved: first-run defaults
            data = env_defaults()
        clean, problems, _ = check(data, mapped_roots())
        return clean, problems, source


def load():
    return load_with_report()[0]


def public(cfg):
    """Settings for the browser: the password itself never leaves the server."""
    out = dict(cfg)
    out["password_set"] = bool(out.pop("password", ""))
    return out


def storage_info():
    """Where settings live and whether they are really being kept."""
    try:
        saved_at = os.path.getmtime(SETTINGS_PATH)
    except OSError:
        saved_at = None
    return {"path": SETTINGS_PATH, "saved_at": saved_at}


def save(data, roots=None):
    """roots: the mapped folders to hold the source/move/output folders to (None = not checked)."""
    data = dict(data)
    # The browser never receives the password: a blank field keeps the saved one, clear_password removes it.
    if data.pop("clear_password", False):
        data["password"] = ""
    elif not data.get("password"):
        data["password"] = load()["password"]
    clean, errors = validate(data, roots)
    if errors:
        return None, errors
    with _lock:
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            tmp = SETTINGS_PATH + ".tmp"
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # may hold the archive password
            with os.fdopen(fd, "w") as f:
                json.dump(clean, f, indent=2)
            os.replace(tmp, SETTINGS_PATH)
            with open(SETTINGS_PATH) as f:        # read it back: prove it was stored
                if json.load(f) != clean:
                    raise OSError("settings file did not read back correctly")
        except (OSError, ValueError) as e:
            return None, [f"Could not store settings in {SETTINGS_PATH} ({e}). "
                          "Check that the App data folder is mapped to a writable folder."]
    return clean, []
