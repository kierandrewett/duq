# space-finder

Find where disk space is being used at a path, fast. Point it at something like
`~/dev` and it tells you what is eating the space, in under a second once warm.

The trick is that it never makes you wait for a walk. Walking a big tree is slow
(a plain `du -sh ~/dev` did not finish in two minutes here, on a cold cache). So
space-finder splits the work: a background scanner keeps a per-path cache warm on
disk, and every read command answers straight from that cache. On my `~/dev`
(618G, ~10 million files) a full scan takes about 2 minutes, and reading the
result back takes under a second.

Stdlib Python only. Nothing to install.

---

## Quick start

```bash
# scan a path once (writes a cache under ~/.cache/space-finder)
./space_finder.py scan ~/dev

# show the breakdown, instantly, from cache
./space_finder.py show ~/dev
./space_finder.py ~/dev            # "show" is the default, so this works too

# drill into any subpath that the scan already covered
./space_finder.py show ~/dev/atlas-2
```

Put it on your PATH if you want a shorter command:

```bash
ln -s "$PWD/space_finder.py" ~/.local/bin/space-finder
space-finder ~/dev
```

---

## Commands

| Command | What it does |
| --- | --- |
| `scan <path>` | Walk the path once and cache the result. |
| `watch <path>` | Rescan forever on an interval to keep the cache warm. |
| `show <path>` | Breakdown of a path's direct children, largest first (default command). |
| `top <path>` | Largest directories found *anywhere* below the path. |
| `files <path>` | Largest individual files below the path. |
| `tui <path>` | Interactive drill-down browser (arrows to move, enter to descend, left to go back). |
| `roots` | List every cached root and how fresh it is. |
| `install-service <path>` | Write a systemd user unit so the scan runs in the background for good. |
| `uninstall-service <path>` | Print the commands to remove that unit. |

Read commands auto-scan on first use if there is no cache yet, so `show` always
works even before you have run `scan`.

Useful flags: `--limit N` (how many rows), `--workers N` (scanner threads,
default 8), `--cross-fs` (follow into other mounted filesystems, off by default),
`--apparent` (apparent file size instead of real disk blocks), `--no-color`.

---

## Always finding: keep the cache warm

Two ways to keep it up to date so a read is always fresh and instant.

Foreground loop, handy for a quick session:

```bash
./space_finder.py watch ~/dev --interval 300
```

Background systemd user service, the set-and-forget option:

```bash
./space_finder.py install-service ~/dev --interval 300
systemctl --user daemon-reload
systemctl --user enable --now space-finder-home-kieran-dev.service
loginctl enable-linger "$USER"   # keep it running after you log out
```

The scanner runs at `nice 10` so it stays out of the way.

---

## How it works

- Threaded `os.scandir` walk. The work is dominated by `stat` syscalls, which
  release the GIL, so threads genuinely speed it up.
- Real disk usage by default (`st_blocks * 512`), matching `du`, not apparent
  file size. Directory inodes are counted too.
- Symlinks are never followed, so no loops and no double counting.
- Hardlinked files are counted once.
- Stays on the starting filesystem by default, so a disk mounted under the path
  is not pulled into the total. Use `--cross-fs` to include it.
- Cache is atomic JSON per root under `~/.cache/space-finder/`: a temp file plus
  rename, so a reader never sees a half-written file.

---

## Notes and limits

- The cache stores the full directory tree plus the 300 largest files. For a tree
  with millions of files that JSON can be tens of MB, which is why reads are a
  fraction of a second rather than instant, but still well inside the target.
- Sizes shown are rounded like `du -h`. The underlying byte counts are exact.
- Deleting or moving files after a scan will not show until the next scan. Run
  `watch` or the service if you want it to keep up on its own.
