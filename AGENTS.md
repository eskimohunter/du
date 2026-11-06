# AGENTS.md

## Repo shape

- `du.py` is the whole app: a single-file tkinter disk-usage treemap. No package manifest, lockfile, build/lint/typecheck config, or CI.
- `smoke_test.py` is an optional stdlib-only smoke test (not part of the app).
- `du.cmd` is the Windows launcher. `readme.md` is the user-facing doc; keep its lowercase filename and update it when behavior changes.
- Python 3.10+, standard library only (tkinter). Do not add third-party dependencies or split into modules unless asked. Windows-first; tested with Python 3.14 / Tk 8.6.

## Commands

- Run directly: `py -3 du.py [folder]`. In this shell `python` can resolve to the Microsoft Store alias and fail; prefer `py -3`.
- Launcher: `du.cmd` (maximized, blank), `du.cmd "C:\path"` (maximized, immediate scan), `du.cmd --help`.
- Syntax check: `py -3 -m py_compile du.py`.
- Smoke tests: `py -3 smoke_test.py`.
- No full test suite. For behavior changes, run the GUI against a temp fixture (nested folders, files of varied sizes to exercise the discrete/bundled split, empty folder/file, junction/symlink, Unicode/space names) and verify scan, zoom, Up boundary, breadcrumb, refresh, both lists, resize, cancel, close, theme toggle, long paths.

## Architecture / invariants

- Order in `du.py`: helpers/squarify -> Scanner -> TreemapView -> ListWindow -> TreemapApp -> main.
- Scanner builds the Node tree in a worker thread; Tk is only touched on the main thread through `queue` + `after` polling. Preserve scan generation/cancel behavior.
- Folder sizes are recursive logical `st_size`; `LARGE_FILE_THRESHOLD` at the top (100) is the number of largest direct files shown discretely; the rest are grouped into an `Other files` box. Hidden/system files are included; symlinks/junctions/mount points are skipped; zero-size items are counted but not drawn.
- Long paths: `normalize_path` applies the `\\?\` prefix on Windows; `strip_long_path_prefix` keeps display/breadcrumb/path-key consistent.
- Treemap is recursive squarified. `Other` aggregation and the compact Other box are intentional; keep the Other button/list usable when the box is tiny or covered.
- Navigation boundary: `tree_root` is the loaded search root. `on_up` must stop there; breadcrumb ancestors above it are muted/non-clickable (`is_within_path`). The path box/Browse start a new search root. Do not restore parent-loading in `on_up` or `_on_breadcrumb_click`.
- Startup is maximized (`root.state("zoomed")`), not fullscreen. F11 toggles fullscreen; Esc cancels a scan first, otherwise exits fullscreen.
- `ListWindow` Treeview rows must set `rowheight` from the UI font linespace + 8; otherwise rows overlap at higher DPI. Applies to both Other files and Other lists.
- `du.cmd` forwards all args (`%*`), prefers `py -3`, falls back to `python`, and returns the Python exit code.
