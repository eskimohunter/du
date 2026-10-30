# du

A small, dependency-free disk-usage treemap browser for Windows, built with Python and tkinter.

`du.py` scans a folder and draws its contents as a nested squarified treemap. Folders are sized by their recursive logical size, large files get their own boxes, and smaller files are grouped per folder. Double-click folders to zoom into them.

## Requirements

- Python 3.10 or newer
- Tkinter (included with the standard Windows Python installer)
- No third-party packages

Tested with Python 3.14 / Tk 8.6 on Windows.

## Files

| File | Purpose |
| --- | --- |
| `du.py` | The application (single file, standard library only) |
| `du.cmd` | Windows launcher; forwards all arguments to `du.py` |

## Usage

```powershell
# Open maximized, then choose a folder
du.cmd

# Open maximized and scan immediately
du.cmd "C:\Users\Robert\Documents"

# Show usage
du.cmd --help
```

Or invoke the script directly:

```powershell
py -3 du.py
py -3 du.py "C:\some\folder"
python du.py
```

If a folder is passed on the command line, it is scanned immediately after the window opens. Without a path, the app starts blank.

`du.cmd` prefers the `py -3` launcher and falls back to `python`. It passes all arguments through and returns the Python process's exit code.

## Features

- Nested squarified treemap with readable folder header strips.
- Recursive folder sizes based on logical file bytes (`st_size`).
- Files at or above `LARGE_FILE_THRESHOLD` get individual boxes; smaller direct files are grouped into one `Small files (N)` box per folder.
- Tiny items are aggregated into an `Other` box. The `Other…` toolbar button opens the aggregate list, and small-files groups that are too small to draw are folded into `Other` and remain reachable there.
- Double-click a folder box to zoom into it.
- Full-path breadcrumb; the current folder is bold, in-tree ancestors are clickable, and ancestors above the loaded search root are muted and non-clickable.
- Up stops at the loaded search root. Use the path box or Browse to load a different folder as a new search root.
- Refresh rescans the loaded root and keeps the current zoomed view when it still exists.
- Background scanning with progress, cancellation, and a responsive UI.
- Small-files and Other list windows with name, type, size, and path columns, sorted by size; rows are sized from the UI font.
- Hidden and system files are included.
- Symlinks, junctions, and mount points are skipped to avoid loops; unreadable items and skipped links are counted.
- Zero-byte files and empty folders are not drawn; they are counted in the status bar.
- Status bar shows totals, hover details, progress, empty/link/unreadable counts, and items too small to draw.

## Controls

| Control | Action |
| --- | --- |
| Enter in path box | Load the typed folder |
| Browse… | Choose a folder |
| Up / Alt+Up / Alt+Left | Move up within the loaded tree; stops at the search root |
| Refresh / F5 | Rescan the loaded search root |
| Double-click folder | Zoom into the folder |
| Double-click Small files / Other | Open the corresponding list |
| Esc | Cancel an active scan; otherwise exit fullscreen if active |
| F11 | Toggle fullscreen |
| Alt+F4 | Close the app |

The app starts **maximized**. `F11` toggles fullscreen; `Esc` exits fullscreen when no scan is active.

## Configuration

The main tuning constants are near the top of `du.py`:

- `LARGE_FILE_THRESHOLD` — default `100 * 1024 * 1024` (100 MiB).
- `MAX_NEST_DEPTH` — maximum nested folder depth to draw before drawing a folder as a leaf.
- `AGG_MIN_W`, `AGG_MIN_H`, `AGG_MIN_AREA`, `OTHER_MIN_W`, `OTHER_MIN_H` — tiny-item aggregation and minimum `Other` box size.
- Layout and color constants (`MARGIN`, `GAP`, `COLOR_*`).

## Notes and limitations

- Sizes are logical file sizes, not allocated/on-disk sizes.
- Hard links may be counted more than once.
- Directory entry overhead is ignored.
- The first scan of a large tree can take time; progress is shown and `Esc` cancels.
- Loading a new folder via the path box or Browse starts a new search root; Up and the breadcrumb cannot go above that root.
- The app is Windows-first. The Python/Tkinter UI should work on other platforms where Tk is available.
