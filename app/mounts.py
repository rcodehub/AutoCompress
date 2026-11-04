"""What folders did the user actually map into this container?

Reads /proc/self/mountinfo, so the Settings page can show the container paths that exist
(the right-hand side of the Unraid "Host path -> Container path" mappings).
"""
import os
import re
import shutil

MOUNTINFO = os.environ.get("MOUNTINFO_PATH", "/proc/self/mountinfo")

# pseudo / system file systems we never want to offer
SKIP_FS = {
    "proc", "sysfs", "tmpfs", "devtmpfs", "devpts", "mqueue", "cgroup", "cgroup2", "overlay",
    "shm", "nsfs", "securityfs", "debugfs", "tracefs", "fusectl", "configfs", "pstore", "bpf",
    "autofs", "binfmt_misc", "hugetlbfs", "rpc_pipefs", "selinuxfs",
}
SKIP_PREFIX = ("/proc", "/sys", "/dev", "/run", "/etc", "/app", "/usr", "/lib", "/bin", "/sbin", "/var/run")
SOURCE_DIR = os.environ.get("SOURCE_DIR", "/source")   # container path of the folder whose sub-folders are compressed
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")   # optional: where the .rar files go
TRASH_DIR = os.environ.get("TRASH_DIR", "/trash")      # optional: where compressed folders are moved
MD_RE = re.compile(r"^/dev/md(\d+)(p\d+)?$")   # Unraid array disk N -> /mnt/diskN
HOST_PATH_RE = re.compile(r"^/mnt/(user0?|disk\d+|cache\w*|remotes|disks|addons)(/|$)")


def _unescape(s):
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def host_path(fstype, source, root):
    """Best guess of the folder on the Unraid server behind a mapping, or None if unknown.
    root = the folder inside the mounted file system that was mapped (from mountinfo)."""
    sub = "" if root in ("", "/") else root
    if fstype == "fuse.shfs":                       # Unraid user shares
        return "/mnt/user" + sub if sub else "/mnt/user"
    m = MD_RE.match(source or "")
    if m:                                           # array disk
        return f"/mnt/disk{m.group(1)}{sub}"
    if fstype == "zfs" and source and not source.startswith("/"):
        return "/mnt/" + source + sub               # ZFS dataset (pool/dataset is mounted at /mnt/pool/dataset)
    return None


def list_mounts(config_dir="/data"):
    """Mapped folders inside the container: [{path, fstype, writable, free, total, config}]."""
    try:
        with open(MOUNTINFO) as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    out, seen = [], set()
    for ln in lines:
        left, sep, right = ln.partition(" - ")
        fields, rest = left.split(), right.split()
        if not sep or len(fields) < 5 or not rest:
            continue
        mp, fstype, root = _unescape(fields[4]), rest[0], _unescape(fields[3])
        source = _unescape(rest[1]) if len(rest) > 1 else ""
        if mp == "/" or mp in seen or fstype in SKIP_FS or mp.startswith(SKIP_PREFIX):
            continue
        if not os.path.isdir(mp):
            continue
        seen.add(mp)
        item = {"path": mp, "fstype": fstype, "config": os.path.normpath(mp) == os.path.normpath(config_dir),
                "host": host_path(fstype, source, root), "device": source, "root": root,
                "writable": os.access(mp, os.W_OK | os.X_OK), "free": None, "total": None}
        try:
            u = shutil.disk_usage(mp)
            item["free"], item["total"] = u.free, u.total
        except OSError:
            pass
        out.append(item)
    return sorted(out, key=lambda m: m["path"])


def containing_mount(path, mounts):
    """The mapped folder that holds `path` (longest match), or None."""
    best = None
    p = os.path.normpath(path)
    for m in mounts:
        mp = os.path.normpath(m["path"])
        if p == mp or p.startswith(mp.rstrip(os.sep) + os.sep):
            if best is None or len(mp) > len(best["path"]):
                best = m
    return best


def path_info(path, mounts):
    """Can the app use this folder? Used for the status line under each source folder."""
    exists = os.path.isdir(path)
    info = {
        "exists": exists,
        "readable": exists and os.access(path, os.R_OK | os.X_OK),
        "writable": exists and os.access(path, os.W_OK | os.X_OK),
        "mount": None,
        "host_path": False,
        "host": None,
    }
    m = containing_mount(path, mounts)
    if m:
        info["mount"] = m["path"]
        if m.get("host"):
            rel = os.path.relpath(os.path.normpath(path), os.path.normpath(m["path"]))
            info["host"] = m["host"] if rel == "." else m["host"].rstrip("/") + "/" + rel
    if not exists and HOST_PATH_RE.match(path):
        info["host_path"] = True   # looks like an Unraid host path, not a container path
    return info


RECYCLE_NAME = ".Recycle.Bin"


def _parts(p):
    return [x for x in p.split("/") if x]


def recycle_target(path, mounts):
    """Where the Unraid Recycle Bin plugin keeps deleted files for `path`: <share>/.Recycle.Bin/<path inside the share>.
    Returns (destination folder, None) or (None, reason). The share is the first folder of the mapped host path
    (from mountinfo), and the share's root has to be reachable inside the container: either the share itself is
    mapped, or all shares (e.g. /mnt/user) are."""
    m = containing_mount(path, mounts)
    if not m:
        return None, f"{path} is not inside a mapped folder"
    rel_in_mount = _parts(os.path.relpath(os.path.normpath(path), os.path.normpath(m["path"])))
    rel_in_mount = [] if rel_in_mount == ["."] else rel_in_mount
    root = _parts(m.get("root") or "")
    if len(root) == 0:
        # a whole file system is mapped (e.g. /mnt/user): the first folder below it is the share
        if not rel_in_mount:
            return None, f"{path} is the top of all shares - choose a folder inside a share"
        share_root = os.path.join(m["path"], rel_in_mount[0])
        inside = rel_in_mount[1:]
    elif len(root) == 1:
        share_root, inside = m["path"], rel_in_mount             # the share itself is mapped
    else:
        # a folder inside a share is mapped: look for another mapping of the same file system that shows the share root
        share = root[0]
        alt = next((o for o in mounts if o is not m and o.get("device") == m.get("device") and o.get("fstype") == m.get("fstype")
                    and not _parts(o.get("root") or "")), None)
        if not alt:
            return None, (f"the share '{share}' is not reachable from {m['path']}: map the share itself "
                          f"(host path /mnt/user/{share}) or all shares (/mnt/user) into the container")
        share_root = os.path.join(alt["path"], share)
        inside = root[1:] + rel_in_mount
    if not inside:
        return None, f"{path} is the share root itself"
    return os.path.join(share_root, RECYCLE_NAME, *inside[:-1]), None


def suggested_sources(config_dir="/data"):
    """First-run sources: /source if it exists, otherwise any other mapped folder
    (never the app data, output or trash folder). Checked on the real file system."""
    if os.path.isdir(SOURCE_DIR):
        return [SOURCE_DIR]
    skip = {os.path.normpath(p) for p in (TRASH_DIR, OUTPUT_DIR, config_dir, "/mnt/user")}
    return [m["path"] for m in list_mounts(config_dir) if os.path.normpath(m["path"]) not in skip]


def suggested_output_dir():
    """/output if it exists in the container (i.e. it was mapped); otherwise '' (archives go next to the folders)."""
    return OUTPUT_DIR if os.path.isdir(OUTPUT_DIR) else ""


def suggested_move_dir():
    """/trash if it exists in the container (i.e. it was mapped); otherwise ''."""
    return TRASH_DIR if os.path.isdir(TRASH_DIR) else ""
