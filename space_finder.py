#!/usr/bin/env python3
"""space-finder: find where disk space is being used at a path, fast.

The design splits the slow part from the fast part. Walking a big tree like
~/dev can take a while (a naive `du -sh` on it did not finish in two minutes on
a cold cache), so we never make the display wait for a walk. A background
scanner keeps a per-root cache warm on disk, and every read command loads that
cache and answers instantly. That is how "show me within 10s" holds even when
the tree is large: the reader reads bytes off disk, it does not walk the tree.

Cache layout (all under ~/.cache/space-finder):
    roots.json          index of scanned roots -> metadata
    <sha1(root)>.json   the size tree + top-files list for one root

Everything here is stdlib only, so there is nothing to install.
"""

from __future__ import annotations

import argparse
import hashlib
import heapq
import json
import os
import queue
import sys
import threading
import time

CACHE_DIR = os.path.join(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")),
    "space-finder",
)
ROOTS_INDEX = os.path.join(CACHE_DIR, "roots.json")

# How many largest individual files to remember per root. Cheap to store and
# the single most useful thing when you are hunting for what to delete.
TOP_FILES = 300

# Cache older than this (seconds) is shown with a staleness warning.
DEFAULT_MAX_AGE = 600


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------

def human_bytes(n):
    """Format bytes the way du -h does: base 1024, one decimal under 10 units."""
    n = float(n)
    for unit in ("B", "K", "M", "G", "T", "P"):
        if abs(n) < 1024.0 or unit == "P":
            if unit == "B":
                return f"{int(n)}{unit}"
            if n < 10:
                return f"{n:.1f}{unit}"
            return f"{n:.0f}{unit}"
        n /= 1024.0


def cache_path_for(root):
    digest = hashlib.sha1(root.encode("utf-8")).hexdigest()
    return os.path.join(CACHE_DIR, digest + ".json")


def atomic_write_json(path, data):
    """Write JSON to a temp file then rename, so a reader never sees a half file."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, path)


def load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def load_roots_index():
    return load_json(ROOTS_INDEX) or {}


def update_roots_index(root, meta):
    idx = load_roots_index()
    idx[root] = meta
    atomic_write_json(ROOTS_INDEX, idx)


def supports_colour(stream):
    return hasattr(stream, "isatty") and stream.isatty()


# --------------------------------------------------------------------------
# scanner
# --------------------------------------------------------------------------

class TopFiles:
    """Thread-safe bounded min-heap of the largest (size, path) files seen.

    The `threshold` read is deliberately lock-free. It is only a hint used to
    skip the lock for the overwhelming majority of files that are too small to
    ever make the list, so a stale read just means we take the lock once more
    than strictly needed. It never corrupts the heap.
    """

    def __init__(self, limit):
        self.limit = limit
        self.lock = threading.Lock()
        self.heap = []  # (size, path)
        self.threshold = 0

    def offer(self, size, path):
        if len(self.heap) >= self.limit and size <= self.threshold:
            return
        with self.lock:
            if len(self.heap) < self.limit:
                heapq.heappush(self.heap, (size, path))
            elif size > self.heap[0][0]:
                heapq.heapreplace(self.heap, (size, path))
            if len(self.heap) >= self.limit:
                self.threshold = self.heap[0][0]

    def sorted_desc(self):
        return [
            {"path": p, "size": s}
            for s, p in sorted(self.heap, key=lambda x: -x[0])
        ]


def scan_tree(root, one_file_system=True, apparent=False, workers=8):
    """Walk `root` with a pool of threads and return (tree_node, stats, top_files).

    Threads help here even with the GIL because the work is dominated by stat
    syscalls, which release the GIL. Symlinks are never followed (avoids loops
    and double counting), hardlinks are counted once, and by default we stay on
    the starting filesystem so a mounted disk under the path is not pulled in.
    """
    root = os.path.abspath(root)
    root_dev = os.stat(root).st_dev

    dirs = {}  # path -> {"own_size", "file_count", "subdirs": [names]}
    struct_lock = threading.Lock()
    seen_hardlinks = set()  # (dev, ino) for files with nlink > 1
    top = TopFiles(TOP_FILES)
    work = queue.Queue()

    dirs[root] = {"own_size": 0, "file_count": 0, "subdirs": []}
    work.put(root)

    def process(path):
        own_size = 0
        file_count = 0
        subdirs = []
        try:
            # count the directory's own blocks too, so totals line up with du
            dst = os.stat(path)
            own_size += dst.st_size if apparent else dst.st_blocks * 512
        except OSError:
            pass
        try:
            entries = list(os.scandir(path))
        except OSError:
            dirs[path]["own_size"] = own_size
            dirs[path]["file_count"] = 0
            dirs[path]["subdirs"] = []
            return
        for entry in entries:
            try:
                if entry.is_symlink():
                    continue
                st = entry.stat(follow_symlinks=False)
                if entry.is_dir(follow_symlinks=False):
                    if one_file_system and st.st_dev != root_dev:
                        continue
                    child = os.path.join(path, entry.name)
                    with struct_lock:
                        dirs[child] = {"own_size": 0, "file_count": 0, "subdirs": []}
                    subdirs.append(entry.name)
                    work.put(child)
                elif entry.is_file(follow_symlinks=False):
                    if st.st_nlink > 1:
                        key = (st.st_dev, st.st_ino)
                        with struct_lock:
                            if key in seen_hardlinks:
                                continue
                            seen_hardlinks.add(key)
                    size = st.st_size if apparent else st.st_blocks * 512
                    own_size += size
                    file_count += 1
                    top.offer(size, os.path.join(path, entry.name))
                # anything else (sockets, fifos, devices) contributes nothing
            except OSError:
                continue
        info = dirs[path]
        info["own_size"] = own_size
        info["file_count"] = file_count
        info["subdirs"] = subdirs

    def worker():
        while True:
            path = work.get()
            if path is None:
                work.task_done()
                break
            try:
                process(path)
            finally:
                work.task_done()

    threads = [threading.Thread(target=worker, daemon=True) for _ in range(workers)]
    for t in threads:
        t.start()
    work.join()
    for _ in threads:
        work.put(None)
    for t in threads:
        t.join()

    tree = _build_tree(root, dirs)
    stats = {
        "total_size": tree["size"],
        "total_files": tree["file_count"],
        "total_dirs": tree["dir_count"] + 1,
    }
    return tree, stats, top.sorted_desc()


def _build_tree(root, dirs):
    """Aggregate the flat per-directory results into a nested size tree.

    Iterative post-order rather than recursion so a deeply nested tree (think
    stacked node_modules) can never blow the Python stack.
    """
    built = {}
    stack = [(root, False)]
    while stack:
        path, processed = stack.pop()
        info = dirs.get(path)
        if info is None:
            continue
        if not processed:
            stack.append((path, True))
            for name in info["subdirs"]:
                stack.append((os.path.join(path, name), False))
        else:
            children = []
            for name in info["subdirs"]:
                node = built.pop(os.path.join(path, name), None)
                if node is not None:
                    children.append(node)
            total = info["own_size"] + sum(c["size"] for c in children)
            files = info["file_count"] + sum(c["file_count"] for c in children)
            subdirs = len(children) + sum(c["dir_count"] for c in children)
            children.sort(key=lambda c: -c["size"])
            built[path] = {
                "name": os.path.basename(path) or path,
                "size": total,
                "own_size": info["own_size"],
                "file_count": files,
                "dir_count": subdirs,
                "children": children,
            }
    return built.get(root, {
        "name": os.path.basename(root) or root,
        "size": 0, "own_size": 0, "file_count": 0, "dir_count": 0, "children": [],
    })


def run_scan(root, one_file_system=True, apparent=False, workers=8, quiet=False):
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        print(f"space-finder: not a directory: {root}", file=sys.stderr)
        return None
    started = time.time()
    tree, stats, top_files = scan_tree(root, one_file_system, apparent, workers)
    duration = time.time() - started
    payload = {
        "root": root,
        "scanned_at": started,
        "duration_sec": round(duration, 3),
        "one_file_system": one_file_system,
        "apparent": apparent,
        "tree": tree,
        "top_files": top_files,
        **stats,
    }
    atomic_write_json(cache_path_for(root), payload)
    update_roots_index(root, {
        "scanned_at": started,
        "duration_sec": round(duration, 3),
        "total_size": stats["total_size"],
        "total_files": stats["total_files"],
        "total_dirs": stats["total_dirs"],
        # remember how this root was scanned so the background daemon can
        # keep rescanning it the same way
        "one_file_system": one_file_system,
        "apparent": apparent,
    })
    if not quiet:
        print(
            f"[scan] {root}  {human_bytes(stats['total_size'])}  "
            f"{stats['total_files']} files  {stats['total_dirs']} dirs  "
            f"in {duration:.1f}s",
            file=sys.stderr,
        )
    return payload


# --------------------------------------------------------------------------
# reading the cache
# --------------------------------------------------------------------------

def find_cached_root(path):
    """Return the cached root that contains `path` (longest matching prefix)."""
    path = os.path.abspath(path)
    best = None
    for root in load_roots_index():
        if path == root or path.startswith(root + os.sep):
            if best is None or len(root) > len(best):
                best = root
    return best


def load_cache(root):
    return load_json(cache_path_for(root))


def descend(tree, root, target):
    """Walk the cached tree down to the node for `target`, or None if absent."""
    target = os.path.abspath(target)
    if target == root:
        return tree
    rel = os.path.relpath(target, root)
    node = tree
    for part in rel.split(os.sep):
        nxt = None
        for child in node.get("children", ()):
            if child["name"] == part:
                nxt = child
                break
        if nxt is None:
            return None
        node = nxt
    return node


def collect_all_dirs(node, prefix, out):
    """Flatten every directory node into (size, path) for a global ranking."""
    for child in node.get("children", ()):
        child_path = os.path.join(prefix, child["name"])
        out.append((child["size"], child_path, child["file_count"]))
        collect_all_dirs(child, child_path, out)


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

class Style:
    def __init__(self, colour):
        self.colour = colour

    def wrap(self, code, text):
        if not self.colour:
            return text
        return f"\033[{code}m{text}\033[0m"

    def dim(self, t):
        return self.wrap("2", t)

    def bold(self, t):
        return self.wrap("1", t)

    def cyan(self, t):
        return self.wrap("36", t)

    def yellow(self, t):
        return self.wrap("33", t)

    def red(self, t):
        return self.wrap("31", t)


def bar(fraction, width=20):
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(fraction * width))
    return "#" * filled + "-" * (width - filled)


def age_note(scanned_at, style):
    age = time.time() - scanned_at
    if age < 90:
        txt = f"{int(age)}s ago"
    elif age < 5400:
        txt = f"{int(age / 60)}m ago"
    else:
        txt = f"{age / 3600:.1f}h ago"
    if age > DEFAULT_MAX_AGE:
        return style.yellow(f"scanned {txt} (stale)")
    return style.dim(f"scanned {txt}")


def print_breakdown(cache, target, limit, style):
    """ncdu-style: direct children of `target`, largest first, with a synthetic
    entry for the bytes held in this directory's own files."""
    root = cache["root"]
    node = descend(cache["tree"], root, target)
    if node is None:
        print(f"space-finder: {target} not in cached scan of {root}", file=sys.stderr)
        print("  run:  space-finder scan " + target, file=sys.stderr)
        return
    rows = [
        (c["size"], c["name"] + "/", c["file_count"])
        for c in node.get("children", ())
    ]
    if node.get("own_size", 0) > 0:
        rows.append((node["own_size"], ". (files here)", None))
    rows.sort(key=lambda r: -r[0])

    total = node["size"] or 1
    biggest = rows[0][0] if rows else 1

    header = f"{style.bold(target)}   {style.cyan(human_bytes(node['size']))} total"
    print(header + "   " + age_note(cache["scanned_at"], style))
    print(style.dim(f"{node['file_count']} files, {node['dir_count']} dirs below here"))
    print()
    for size, name, fcount in rows[:limit]:
        pct = 100.0 * size / total
        frac = size / biggest if biggest else 0
        meta = f"{fcount} files" if fcount is not None else ""
        line = (
            f"{human_bytes(size):>7}  {pct:5.1f}%  "
            f"[{style.cyan(bar(frac))}]  {name}"
        )
        if meta:
            line += "  " + style.dim(meta)
        print(line)
    if len(rows) > limit:
        print(style.dim(f"... {len(rows) - limit} more (use --limit)"))


def print_deep(cache, target, limit, style):
    """Largest directories found anywhere below the target, not just direct kids."""
    root = cache["root"]
    node = descend(cache["tree"], root, target)
    if node is None:
        print(f"space-finder: {target} not in cached scan of {root}", file=sys.stderr)
        return
    out = []
    collect_all_dirs(node, target, out)
    out.sort(key=lambda r: -r[0])
    print(f"{style.bold('Largest directories under ' + target)}   "
          + age_note(cache["scanned_at"], style))
    print()
    biggest = out[0][0] if out else 1
    home = os.path.expanduser("~")
    for size, path, fcount in out[:limit]:
        shown = path.replace(home, "~", 1) if path.startswith(home) else path
        frac = size / biggest if biggest else 0
        print(
            f"{human_bytes(size):>7}  [{style.cyan(bar(frac))}]  "
            f"{shown}  {style.dim(str(fcount) + ' files')}"
        )


def print_files(cache, limit, style):
    top = cache.get("top_files", [])
    print(f"{style.bold('Largest files in ' + cache['root'])}   "
          + age_note(cache["scanned_at"], style))
    print()
    if not top:
        print(style.dim("(none recorded)"))
        return
    biggest = top[0]["size"] if top else 1
    home = os.path.expanduser("~")
    for item in top[:limit]:
        path = item["path"]
        shown = path.replace(home, "~", 1) if path.startswith(home) else path
        frac = item["size"] / biggest if biggest else 0
        print(f"{human_bytes(item['size']):>7}  [{style.cyan(bar(frac))}]  {shown}")


# --------------------------------------------------------------------------
# interactive browser
# --------------------------------------------------------------------------

def run_tui(cache, start_path):
    import curses

    root = cache["root"]
    start = descend(cache["tree"], root, start_path) or cache["tree"]

    def draw(stdscr, node, path):
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        rows = [(c["size"], c["name"] + "/", c) for c in node.get("children", ())]
        if node.get("own_size", 0) > 0:
            rows.append((node["own_size"], ". (files here)", None))
        rows.sort(key=lambda r: -r[0])
        return rows

    def loop(stdscr):
        curses.curs_set(0)
        stack = []  # (node, path, selection)
        node, path, sel = start, start_path, 0
        while True:
            rows = draw(stdscr, node, path)
            h, w = stdscr.getmaxyx()
            total = node["size"] or 1
            home = os.path.expanduser("~")
            shown_path = path.replace(home, "~", 1) if path.startswith(home) else path
            stdscr.addnstr(0, 0, f" {shown_path}  ({human_bytes(node['size'])})".ljust(w), w, curses.A_REVERSE)
            stdscr.addnstr(1, 0, " up/down move  enter descend  left back  q quit", w)
            top = max(0, sel - (h - 4))
            for i, (size, name, child) in enumerate(rows[top:top + h - 3]):
                idx = top + i
                pct = 100.0 * size / total
                line = f"{human_bytes(size):>7} {pct:5.1f}% [{bar(size / (rows[0][0] or 1), 16)}] {name}"
                attr = curses.A_REVERSE if idx == sel else curses.A_NORMAL
                stdscr.addnstr(3 + i, 0, line.ljust(w), w, attr)
            stdscr.refresh()
            key = stdscr.getch()
            if key in (ord("q"), 27):
                return
            if key in (curses.KEY_DOWN, ord("j")) and rows:
                sel = min(sel + 1, len(rows) - 1)
            elif key in (curses.KEY_UP, ord("k")) and rows:
                sel = max(sel - 1, 0)
            elif key in (curses.KEY_RIGHT, curses.KEY_ENTER, 10, 13, ord("l")):
                if rows and rows[sel][2] is not None:
                    child = rows[sel][2]
                    stack.append((node, path, sel))
                    node, path, sel = child, os.path.join(path, child["name"]), 0
            elif key in (curses.KEY_LEFT, ord("h"), curses.KEY_BACKSPACE, 127):
                if stack:
                    node, path, sel = stack.pop()

    curses.wrapper(loop)


# --------------------------------------------------------------------------
# background watch + systemd
# --------------------------------------------------------------------------

def run_watch(root, interval, one_file_system, apparent, workers):
    root = os.path.abspath(root)
    try:
        os.nice(10)  # be a good citizen, this runs forever in the background
    except (OSError, AttributeError):
        pass
    print(f"[watch] scanning {root} every {interval}s (nice)", file=sys.stderr)
    while True:
        started = time.time()
        try:
            run_scan(root, one_file_system, apparent, workers, quiet=False)
        except Exception as exc:  # keep the loop alive across transient errors
            print(f"[watch] scan failed: {exc}", file=sys.stderr)
        slept = interval - (time.time() - started)
        if slept > 0:
            time.sleep(slept)


def run_watch_all(interval, workers):
    """Keep every cached root warm. This is the "run it from any dir" daemon:
    anything you scan or show gets registered in the roots index, and this loop
    rescans them all, each with the options it was originally scanned with."""
    try:
        os.nice(10)
    except (OSError, AttributeError):
        pass
    print(f"[watch-all] keeping all cached roots warm, every {interval}s (nice)",
          file=sys.stderr)
    while True:
        started = time.time()
        idx = load_roots_index()
        if not idx:
            print("[watch-all] no roots yet; run: space-finder scan <path>",
                  file=sys.stderr)
        for root, meta in idx.items():
            if not os.path.isdir(root):
                print(f"[watch-all] skip missing {root}", file=sys.stderr)
                continue
            try:
                run_scan(
                    root,
                    meta.get("one_file_system", True),
                    meta.get("apparent", False),
                    workers,
                    quiet=False,
                )
            except Exception as exc:  # one bad root must not kill the loop
                print(f"[watch-all] scan failed for {root}: {exc}", file=sys.stderr)
        slept = interval - (time.time() - started)
        if slept > 0:
            time.sleep(slept)


def forget_root(path):
    """Drop a root from the index and delete its cache file."""
    root = os.path.abspath(path)
    idx = load_roots_index()
    if root not in idx:
        print(f"space-finder: {root} is not a cached root", file=sys.stderr)
        print("  cached roots:  space-finder roots", file=sys.stderr)
        return
    del idx[root]
    atomic_write_json(ROOTS_INDEX, idx)
    try:
        os.remove(cache_path_for(root))
    except OSError:
        pass
    print(f"[forget] removed {root}")


def service_slug(root):
    slug = root.strip("/").replace("/", "-").replace(" ", "_") or "root"
    return f"space-finder-{slug}"


def install_service(root, interval, one_file_system, apparent, workers):
    root = os.path.abspath(root)
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    name = service_slug(root)
    script = os.path.abspath(__file__)
    py = sys.executable
    flags = f"--interval {interval} --workers {workers}"
    if not one_file_system:
        flags += " --cross-fs"
    if apparent:
        flags += " --apparent"
    unit = f"""[Unit]
Description=space-finder background scan of {root}
After=default.target

[Service]
Type=simple
ExecStart={py} {script} watch {root} {flags}
Nice=10
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""
    unit_path = os.path.join(unit_dir, name + ".service")
    with open(unit_path, "w", encoding="utf-8") as fh:
        fh.write(unit)
    print(f"[service] wrote {unit_path}")
    print("[service] enable and start it with:")
    print(f"    systemctl --user daemon-reload")
    print(f"    systemctl --user enable --now {name}.service")
    print(f"    loginctl enable-linger {os.environ.get('USER', '')}   # survive logout")


def install_global_service(interval, workers):
    """Install a single service that keeps every cached root warm."""
    unit_dir = os.path.expanduser("~/.config/systemd/user")
    os.makedirs(unit_dir, exist_ok=True)
    name = "space-finder"
    script = os.path.abspath(__file__)
    py = sys.executable
    unit = f"""[Unit]
Description=space-finder background scan of all cached roots
After=default.target

[Service]
Type=simple
ExecStart={py} {script} watch-all --interval {interval} --workers {workers}
Nice=10
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
"""
    unit_path = os.path.join(unit_dir, name + ".service")
    with open(unit_path, "w", encoding="utf-8") as fh:
        fh.write(unit)
    print(f"[service] wrote {unit_path}")
    print("[service] this keeps every path you scan warm, from any dir. enable it:")
    print(f"    systemctl --user daemon-reload")
    print(f"    systemctl --user enable --now {name}.service")
    print(f"    loginctl enable-linger {os.environ.get('USER', '')}   # survive logout")


def uninstall_service(root=None):
    name = "space-finder" if root is None else service_slug(os.path.abspath(root))
    unit_path = os.path.expanduser(f"~/.config/systemd/user/{name}.service")
    print(f"[service] stop and remove with:")
    print(f"    systemctl --user disable --now {name}.service")
    print(f"    rm {unit_path}")
    print(f"    systemctl --user daemon-reload")


def print_roots(style):
    idx = load_roots_index()
    if not idx:
        print("No roots scanned yet. Try:  space-finder scan ~/dev")
        return
    print(style.bold("Cached roots:"))
    for root, meta in sorted(idx.items(), key=lambda kv: -kv[1].get("total_size", 0)):
        print(
            f"  {human_bytes(meta.get('total_size', 0)):>7}  "
            f"{root}  {style.dim(age_note(meta.get('scanned_at', 0), style))}  "
            f"{style.dim(str(meta.get('total_files', 0)) + ' files')}"
        )


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------

def ensure_cache(path, args, style):
    """Return a fresh-enough cache for `path`, scanning first if we must."""
    root = find_cached_root(path)
    if root is None:
        print(style.dim(f"no cache for {path}, scanning once..."), file=sys.stderr)
        payload = run_scan(
            path, not args.cross_fs, args.apparent, args.workers, quiet=False
        )
        return payload
    cache = load_cache(root)
    if cache is None:
        payload = run_scan(
            root, not args.cross_fs, args.apparent, args.workers, quiet=False
        )
        return payload
    return cache


def add_scan_flags(p):
    p.add_argument("--cross-fs", action="store_true",
                   help="cross into other mounted filesystems (default: stay on one)")
    p.add_argument("--apparent", action="store_true",
                   help="count apparent file size instead of disk blocks used")
    p.add_argument("--workers", type=int, default=8,
                   help="scanner threads (default 8)")


def build_parser():
    p = argparse.ArgumentParser(
        prog="space-finder",
        description="Find where disk space is being used at a path, fast.",
    )
    p.add_argument("-C", "--no-color", action="store_true",
                   help="disable coloured output (accepted anywhere on the line)")
    sub = p.add_subparsers(dest="cmd")

    sp = sub.add_parser("scan", help="walk a path once and cache the result")
    sp.add_argument("path")
    add_scan_flags(sp)

    wp = sub.add_parser("watch", help="rescan one path forever to keep its cache warm")
    wp.add_argument("path")
    wp.add_argument("--interval", type=int, default=300,
                    help="seconds between scans (default 300)")
    add_scan_flags(wp)

    wap = sub.add_parser("watch-all",
                         help="rescan every cached root forever (the any-dir daemon)")
    wap.add_argument("--interval", type=int, default=300,
                     help="seconds between scan cycles (default 300)")
    wap.add_argument("--workers", type=int, default=8, help="scanner threads")

    shp = sub.add_parser("show", help="breakdown of a path from cache (default)")
    shp.add_argument("path", nargs="?", default=".")
    shp.add_argument("--limit", type=int, default=25)
    add_scan_flags(shp)

    tp = sub.add_parser("top", help="largest directories anywhere under a path")
    tp.add_argument("path", nargs="?", default=".")
    tp.add_argument("--limit", type=int, default=25)
    add_scan_flags(tp)

    fp = sub.add_parser("files", help="largest individual files under a path")
    fp.add_argument("path", nargs="?", default=".")
    fp.add_argument("--limit", type=int, default=25)
    add_scan_flags(fp)

    ip = sub.add_parser("tui", help="interactive drill-down browser")
    ip.add_argument("path", nargs="?", default=".")
    add_scan_flags(ip)

    sub.add_parser("roots", help="list cached roots")

    fgp = sub.add_parser("forget", help="drop a cached root and its cache file")
    fgp.add_argument("path")

    isp = sub.add_parser(
        "install-service",
        help="write a systemd user unit; no path = the any-dir daemon for all roots",
    )
    isp.add_argument("path", nargs="?",
                     help="omit to install one service that watches all cached roots")
    isp.add_argument("--interval", type=int, default=300)
    isp.add_argument("--workers", type=int, default=8)
    isp.add_argument("--cross-fs", action="store_true")
    isp.add_argument("--apparent", action="store_true")

    usp = sub.add_parser("uninstall-service",
                         help="print commands to remove a unit (no path = the any-dir daemon)")
    usp.add_argument("path", nargs="?")

    return p


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # --no-color / -C is accepted anywhere on the line, before or after the
    # subcommand, so pull it out first rather than fighting argparse ordering.
    no_color = False
    for flag in ("--no-color", "-C"):
        if flag in argv:
            no_color = True
            argv = [a for a in argv if a != flag]

    # Allow "space-finder ~/dev" as a shortcut for "space-finder show ~/dev".
    known = {"scan", "watch", "watch-all", "show", "top", "files", "tui", "roots",
             "forget", "install-service", "uninstall-service"}
    if argv and not argv[0].startswith("-") and argv[0] not in known:
        argv.insert(0, "show")

    args = build_parser().parse_args(argv)
    style = Style(colour=not no_color and supports_colour(sys.stdout))
    cmd = args.cmd or "show"

    if cmd == "scan":
        run_scan(args.path, not args.cross_fs, args.apparent, args.workers)
        return 0

    if cmd == "watch":
        run_watch(args.path, args.interval, not args.cross_fs, args.apparent, args.workers)
        return 0

    if cmd == "watch-all":
        run_watch_all(args.interval, args.workers)
        return 0

    if cmd == "roots":
        print_roots(style)
        return 0

    if cmd == "forget":
        forget_root(args.path)
        return 0

    if cmd == "install-service":
        if args.path:
            install_service(args.path, args.interval, not args.cross_fs,
                            args.apparent, args.workers)
        else:
            install_global_service(args.interval, args.workers)
        return 0

    if cmd == "uninstall-service":
        uninstall_service(args.path)
        return 0

    # read commands share cache handling
    path = os.path.abspath(getattr(args, "path", ".") or ".")
    cache = ensure_cache(path, args, style)
    if cache is None:
        return 1

    if cmd == "show":
        print_breakdown(cache, path, args.limit, style)
    elif cmd == "top":
        print_deep(cache, path, args.limit, style)
    elif cmd == "files":
        print_files(cache, args.limit, style)
    elif cmd == "tui":
        run_tui(cache, path)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
