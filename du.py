"""du.py - a dependency-free tkinter disk-usage treemap browser.

Run with:
    python du.py [folder]

If a folder is supplied, it is scanned immediately after the window opens.
"""

from __future__ import annotations

import ctypes
import math
import os
import queue
import stat
import subprocess
import sys
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
from tkinter import filedialog, font as tkfont, messagebox, ttk
from typing import Callable, Sequence

APP_TITLE = "du – Disk Usage"

# Files at or above this size get their own box. Everything smaller is grouped
# into one "Small files" box per directory.
LARGE_FILE_THRESHOLD = 100 * 1024 * 1024  # 100 MiB

MAX_NEST_DEPTH = 8
POLL_MS = 100
RESIZE_DEBOUNCE_MS = 120
PROGRESS_INTERVAL_S = 0.10

MARGIN = 8
GAP = 2
FOLDER_HEADER_PAD = 4
FOLDER_CONTENT_PAD = 2
MIN_FOLDER_W = 48
MIN_FOLDER_CONTENT_W = 30
MIN_FOLDER_CONTENT_H = 24
AGG_MIN_W = 28
AGG_MIN_H = 22
AGG_MIN_AREA = AGG_MIN_W * AGG_MIN_H
OTHER_MIN_W = 48
OTHER_MIN_H = 24

COLOR_CANVAS = "#F4F5F7"
COLOR_FOLDER_BODY = "#DCE8F0"
COLOR_FOLDER_LEAF = "#C4DCE8"
COLOR_FOLDER_HEADER = "#3E6B85"
COLOR_FOLDER_OUTLINE = "#2F556B"
COLOR_HEADER_TEXT = "#FFFFFF"
COLOR_FILE = "#F2B35D"
COLOR_FILE_OUTLINE = "#B7791F"
COLOR_GROUP = "#C9CDD2"
COLOR_GROUP_OUTLINE = "#7A7F85"
COLOR_OTHER = "#B9A7D6"
COLOR_OTHER_OUTLINE = "#6B5B95"
COLOR_TEXT = "#1A1A1A"
COLOR_MUTED = "#5F6368"
COLOR_LINK = "#1A73E8"
COLOR_HOVER = "#D7263D"


class ScanCancelled(Exception):
    """Raised inside the scan worker when the user cancels."""


@dataclass(slots=True)
class Node:
    """One item in the scanned tree.

    kind is one of "dir", "file", "group" or "other".
    Directories own their children. Files, groups and synthetic "other" nodes
    are leaves as far as the tree is concerned.
    """

    name: str
    path: str
    size: int
    kind: str
    parent: Node | None = None
    children: list[Node] = field(default_factory=list)
    file_count: int = 0
    dir_count: int = 0
    small_count: int = 0
    small_size: int = 0
    error_count: int = 0
    link_count: int = 0
    empty_file_count: int = 0
    empty_dir_count: int = 0
    scan_failed: bool = False


Row = tuple[str, str, int, str, Node]
Loader = Callable[[threading.Event], tuple[list[Row], int, int, str | None] | None]


@dataclass(slots=True)
class ScanStats:
    files: int = 0
    bytes: int = 0
    last_emit: float = 0.0


@dataclass(slots=True)
class ScanResult:
    root: Node
    nodes_by_path: dict[str, Node]
    elapsed: float


@dataclass(frozen=True, slots=True)
class Rect:
    x: float
    y: float
    w: float
    h: float


def _worst_ratio(row: Sequence[float], side: float) -> float:
    total = sum(row)
    if not row or total <= 0 or side <= 0:
        return float("inf")
    smallest = min(row)
    if smallest <= 0:
        return float("inf")
    side2 = side * side
    return max(
        side2 * max(row) / (total * total),
        (total * total) / (side2 * smallest),
    )


def _layout_row(
    row: Sequence[float],
    x: float,
    y: float,
    w: float,
    h: float,
    rects: list[Rect],
) -> tuple[float, float, float, float]:
    total = sum(row)
    if total <= 0 or w <= 0 or h <= 0:
        return x, y, w, h
    if w >= h:
        thickness = total / h
        yy = y
        for area in row:
            item_h = area / thickness if thickness > 0 else 0.0
            rects.append(Rect(x, yy, thickness, item_h))
            yy += item_h
        return x + thickness, y, max(0.0, w - thickness), h
    thickness = total / w
    xx = x
    for area in row:
        item_w = area / thickness if thickness > 0 else 0.0
        rects.append(Rect(xx, y, item_w, thickness))
        xx += item_w
    return x, y + thickness, w, max(0.0, h - thickness)


def squarify(
    sizes: Sequence[float],
    x: float,
    y: float,
    w: float,
    h: float,
) -> list[Rect]:
    """Lay out rectangles with areas proportional to sizes, largest first.

    The caller is expected to pass sizes in descending order.
    """
    if not sizes or w <= 0 or h <= 0:
        return []
    total = sum(sizes)
    if total <= 0:
        return []
    scale = (w * h) / total
    areas = [float(size) * scale for size in sizes]
    rects: list[Rect] = []
    row: list[float] = []
    index = 0
    while index < len(areas) or row:
        if w <= 0 or h <= 0:
            for _ in range(len(row) + (len(areas) - index)):
                rects.append(Rect(x, y, 0.0, 0.0))
            break
        if index >= len(areas):
            _layout_row(row, x, y, w, h, rects)
            row = []
            break
        area = areas[index]
        if not row:
            row.append(area)
            index += 1
            continue
        side = min(w, h)
        if _worst_ratio(row + [area], side) <= _worst_ratio(row, side):
            row.append(area)
            index += 1
        else:
            x, y, w, h = _layout_row(row, x, y, w, h, rects)
            row = []
    return rects


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size:,} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB", "TiB", "PiB", "EiB", "ZiB", "YiB"):
        value /= 1024.0
        if value < 1024.0 or unit == "YB":
            if value >= 100:
                return f"{value:.0f} {unit}"
            if value >= 10:
                return f"{value:.1f} {unit}"
            return f"{value:.2f} {unit}"
    return f"{size:,} B"


def format_count(value: int) -> str:
    return f"{value:,}"


def format_plural(count: int, singular: str, plural: str | None = None) -> str:
    word = singular if count == 1 else (plural if plural is not None else singular + "s")
    return f"{format_count(count)} {word}"


def sanitize_name(name: str) -> str:
    return name.replace("\n", " ").replace("\r", " ").replace("\t", " ")


def truncate_middle(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len < 8:
        return text[:max_len]
    keep = (max_len - 1) // 2
    return text[:keep] + "…" + text[-(max_len - keep - 1):]


def truncate_text(font: tkfont.Font, text: str, max_width: float) -> str:
    if not text or max_width <= 0:
        return ""
    if font.measure(text) <= max_width:
        return text
    ellipsis = "…"
    if font.measure(ellipsis) > max_width:
        return ""
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid] + ellipsis
        if font.measure(candidate) <= max_width:
            low = mid
        else:
            high = mid - 1
    return (text[:low] + ellipsis) if low else ellipsis


def display_name(path: str) -> str:
    path = strip_long_path_prefix(path)
    stripped = path.rstrip("\\/")
    name = os.path.basename(stripped)
    return name or path


def clean_path(path: str) -> str:
    """Normalize a path.

    os.path.normpath already drops a trailing separator, so no special-casing
    for UNC share roots is needed.
    """
    return os.path.normpath(path)


def _long_path_prefix() -> str:
    return "\\\\?\\" if is_windows() else ""


def strip_long_path_prefix(path: str) -> str:
    prefix = _long_path_prefix()
    if prefix and path.startswith(prefix):
        return path[len(prefix):]
    return path


def to_long_path(path: str) -> str:
    if not is_windows():
        return path
    if path.startswith("\\\\?\\"):
        return path
    # UNC paths use the \\?\UNC\ form.
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path.lstrip("\\")
    if len(path) >= 260:
        return "\\\\?\\" + path
    return path


def normalize_path(raw: str) -> str:
    if not raw:
        return ""
    text = raw.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    if not text:
        return ""
    text = os.path.expandvars(os.path.expanduser(text))
    path = clean_path(os.path.abspath(text))
    return to_long_path(path)


def path_key(path: str) -> str:
    path = strip_long_path_prefix(path)
    return os.path.normcase(os.path.abspath(path))


def is_within_path(path: str, root: str) -> bool:
    """True if path is root itself or a descendant of root."""
    path = strip_long_path_prefix(path)
    root = strip_long_path_prefix(root)
    candidate = os.path.normcase(os.path.abspath(path))
    boundary = os.path.normcase(os.path.abspath(root))
    if candidate == boundary:
        return True
    if boundary.endswith(("\\", "/")):
        return candidate.startswith(boundary)
    return candidate.startswith(boundary + os.sep)


def is_windows() -> bool:
    return os.name == "nt"


def open_in_explorer(path: str) -> None:
    """Open Windows Explorer with the given path selected/highlighted."""
    if not is_windows():
        return
    path = strip_long_path_prefix(path)
    if not path:
        return
    try:
        if os.path.isdir(path):
            subprocess.Popen(["explorer", path])
        else:
            subprocess.Popen(["explorer", f"/select,{path}"])
    except OSError:
        pass


def enable_dpi_awareness() -> None:
    if not is_windows():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def load_small_files(
    folder_path: str,
    cancel: threading.Event,
    threshold: int,
) -> tuple[list[Row], int, int, str | None] | None:
    """Load the direct files below threshold in folder_path.

    Returns (rows, total_size, error_count, fatal_error_message), or None when
    cancelled.
    """
    rows: list[Row] = []
    total = 0
    errors = 0
    try:
        with os.scandir(folder_path) as entries:
            for entry in entries:
                if cancel.is_set():
                    return None
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_file(follow_symlinks=False):
                        st = entry.stat(follow_symlinks=False)
                        if st.st_size < threshold:
                            node = Node(
                                name=entry.name,
                                path=entry.path,
                                size=st.st_size,
                                kind="file",
                            )
                            rows.append(
                                (entry.name, "File", st.st_size, entry.path, node)
                            )
                            total += st.st_size
                except OSError:
                    errors += 1
    except OSError as exc:
        return [], 0, 0, str(exc)
    return rows, total, errors, None


class Scanner:
    """Recursively build a Node tree without touching Tk."""

    def __init__(self, threshold: int) -> None:
        self.threshold = threshold

    def scan(
        self,
        root_path: str,
        cancel: threading.Event,
        progress: Callable[[str, ScanStats], None],
    ) -> ScanResult:
        start = time.monotonic()
        stats = ScanStats(last_emit=start)
        index: dict[str, Node] = {}
        progress(root_path, stats)
        root = self._scan_dir(
            root_path,
            display_name(root_path),
            None,
            cancel,
            progress,
            stats,
            start,
            index,
        )
        return ScanResult(
            root=root,
            nodes_by_path=index,
            elapsed=time.monotonic() - start,
        )

    def _scan_dir(
        self,
        path: str,
        name: str,
        parent: Node | None,
        cancel: threading.Event,
        progress: Callable[[str, ScanStats], None],
        stats: ScanStats,
        start: float,
        index: dict[str, Node],
    ) -> Node:
        if cancel.is_set():
            raise ScanCancelled
        node = Node(name=name, path=path, size=0, kind="dir", parent=parent)
        index[path_key(path)] = node
        try:
            with os.scandir(path) as entries:
                for entry in entries:
                    if cancel.is_set():
                        raise ScanCancelled
                    try:
                        if entry.is_symlink():
                            node.link_count += 1
                            self._maybe_progress(progress, entry.path, stats, cancel)
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            if self._is_link_dir(entry):
                                node.link_count += 1
                            else:
                                child = self._scan_dir(
                                    entry.path,
                                    entry.name,
                                    node,
                                    cancel,
                                    progress,
                                    stats,
                                    start,
                                    index,
                                )
                                node.children.append(child)
                                node.size += child.size
                                node.file_count += child.file_count
                                node.dir_count += 1 + child.dir_count
                                node.error_count += child.error_count
                                node.link_count += child.link_count
                                node.empty_file_count += child.empty_file_count
                                node.empty_dir_count += child.empty_dir_count
                                if child.size == 0 and child.error_count == 0:
                                    node.empty_dir_count += 1
                        elif entry.is_file(follow_symlinks=False):
                            st = entry.stat(follow_symlinks=False)
                            size = st.st_size
                            node.size += size
                            node.file_count += 1
                            stats.files += 1
                            stats.bytes += size
                            if size == 0:
                                node.empty_file_count += 1
                            if size >= self.threshold:
                                node.children.append(
                                    Node(
                                        name=entry.name,
                                        path=entry.path,
                                        size=size,
                                        kind="file",
                                        parent=node,
                                    )
                                )
                            else:
                                node.small_count += 1
                                node.small_size += size
                        else:
                            node.link_count += 1
                    except OSError:
                        node.error_count += 1
                    self._maybe_progress(progress, entry.path, stats, cancel)
        except OSError:
            node.error_count += 1
            node.scan_failed = True
        if node.small_count:
            node.children.append(
                Node(
                    name="Small files",
                    path=path,
                    size=node.small_size,
                    kind="group",
                    parent=node,
                    small_count=node.small_count,
                )
            )
        return node

    def _is_link_dir(self, entry: os.DirEntry) -> bool:
        """True for junction/symlink/mount-point directories.

        Other reparse directories (for example cloud placeholders) are treated
        as ordinary directories so their contents still count.
        """
        if not is_windows():
            return False
        try:
            is_junction = getattr(entry, "is_junction", None)
            if is_junction is not None and is_junction():
                return True
        except OSError:
            pass
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            return False
        attrs = getattr(st, "st_file_attributes", 0)
        flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if not (flag and attrs & flag):
            return False
        tag = getattr(st, "st_reparse_tag", 0)
        link_tags = (
            getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1),
            getattr(stat, "IO_REPARSE_TAG_SYMLINK", -1),
        )
        return tag in link_tags

    @staticmethod
    def _maybe_progress(
        progress: Callable[[str, ScanStats], None],
        path: str,
        stats: ScanStats,
        cancel: threading.Event | None = None,
    ) -> None:
        if cancel is not None and cancel.is_set():
            return
        now = time.monotonic()
        if now - stats.last_emit >= PROGRESS_INTERVAL_S:
            stats.last_emit = now
            progress(path, stats)


class TreemapView(tk.Canvas):
    """Canvas that draws a nested squarified treemap."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        on_zoom: Callable[[Node], None],
        on_group: Callable[[Node], None],
        on_other: Callable[[Node], None],
        on_hover: Callable[[Node], None],
        on_leave: Callable[[], None],
        on_stats: Callable[[int], None],
    ) -> None:
        super().__init__(
            master,
            bg=COLOR_CANVAS,
            highlightthickness=0,
            takefocus=True,
        )
        self.on_zoom = on_zoom
        self.on_group = on_group
        self.on_other = on_other
        self.on_hover = on_hover
        self.on_leave = on_leave
        self.on_stats = on_stats

        base = tkfont.nametofont("TkDefaultFont")
        self.ui_font = base
        self.leaf_font = base.copy()
        self.bold_font = base.copy()
        self.bold_font.configure(weight="bold")
        self.header_font = base.copy()
        self.header_font.configure(weight="bold")
        self.message_font = base.copy()
        try:
            base_size = abs(int(base.cget("size")))
        except (TypeError, ValueError):
            base_size = 10
        self.message_font.configure(size=base_size + 6, weight="bold")
        self.message_small_font = base.copy()
        self._header_h = max(18, self.header_font.metrics("linespace") + 6)

        self.view_root: Node | None = None
        self._message_title = ""
        self._message_subtitle = ""
        self._item_nodes: dict[int, Node] = {}
        self._primary_rects: dict[int, int] = {}
        self._rect_styles: dict[int, tuple[str, int]] = {}
        self._hovered_node: Node | None = None
        self._hovered_restore: tuple[int, str, int] | None = None
        self._resize_job: str | None = None
        self._too_small = 0
        self.root_other: Node | None = None
        self._context_menu: tk.Menu | None = None

        self.bind("<Configure>", self._on_configure)
        self.bind("<Motion>", self._on_motion)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Double-Button-1>", self._on_double_click)
        self.bind("<Button-1>", self._on_button)
        self.bind("<Button-3>", self._on_right_click)
        self.bind("<Destroy>", self._on_destroy, add="+")

    def set_root(self, node: Node) -> None:
        self.view_root = node
        self._message_title = ""
        self._message_subtitle = ""
        self.redraw()

    def show_message(self, title: str, subtitle: str = "") -> None:
        self.view_root = None
        self._message_title = title
        self._message_subtitle = subtitle
        self.redraw()

    def redraw(self) -> None:
        self._reset_hover()
        if self._context_menu is not None:
            try:
                self._context_menu.destroy()
            except tk.TclError:
                pass
            self._context_menu = None
        try:
            self.configure(cursor="")
        except tk.TclError:
            return
        self.on_leave()
        self.delete("all")
        self._item_nodes.clear()
        self._primary_rects.clear()
        self._rect_styles.clear()
        self._too_small = 0
        self.root_other = None

        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 1 or height <= 1:
            self._emit_stats()
            return
        if self.view_root is None:
            self._draw_message()
            self._emit_stats()
            return
        if self.view_root.size <= 0 or not any(
            child.size > 0 for child in self.view_root.children
        ):
            self._draw_message(
                "No data to display",
                "This folder is empty, or contains only empty items.",
            )
            self._emit_stats()
            return

        rect = Rect(
            MARGIN,
            MARGIN,
            max(0.0, width - 2 * MARGIN),
            max(0.0, height - 2 * MARGIN),
        )
        self._layout_children(self.view_root, rect, 0)
        self._emit_stats()

    def _emit_stats(self) -> None:
        self.on_stats(self._too_small)

    def _draw_message(self, title: str | None = None, subtitle: str | None = None) -> None:
        title = self._message_title if title is None else title
        subtitle = self._message_subtitle if subtitle is None else subtitle
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        if not title:
            title = "Choose a folder to analyse"
        self.create_text(
            width / 2,
            height / 2 - 16,
            text=title,
            font=self.message_font,
            fill=COLOR_TEXT,
            justify="center",
            width=max(40, width - 80),
        )
        if subtitle:
            self.create_text(
                width / 2,
                height / 2 + 18,
                text=subtitle,
                font=self.message_small_font,
                fill=COLOR_MUTED,
                justify="center",
                width=max(40, width - 80),
            )

    def _layout_children(self, parent: Node, rect: Rect, depth: int) -> None:
        if rect.w <= 0 or rect.h <= 0:
            return
        children = [child for child in parent.children if child.size > 0]
        if not children:
            return
        children.sort(key=lambda node: (-node.size, node.name.lower()))

        layout_nodes = children
        other: Node | None = None
        for _ in range(2):
            rects = squarify(
                [node.size for node in layout_nodes],
                rect.x,
                rect.y,
                rect.w,
                rect.h,
            )
            tiny: list[Node] = []
            for node, child_rect in zip(layout_nodes, rects):
                if node.kind == "other":
                    continue
                if self._is_tiny(child_rect):
                    tiny.append(node)
            if len(tiny) < 2 and not (
                len(tiny) == 1 and tiny[0].kind == "group"
            ):
                break
            if other is None:
                other = Node(
                    name="Other",
                    path=parent.path,
                    size=0,
                    kind="other",
                    parent=parent,
                )
            tiny_ids = {id(node) for node in tiny}
            other.children.extend(tiny)
            other.size = sum(child.size for child in other.children)
            layout_nodes = [
                node
                for node in layout_nodes
                if id(node) not in tiny_ids and node.kind != "other"
            ]
            layout_nodes.append(other)
            layout_nodes.sort(key=lambda node: (-node.size, node.name.lower()))

        if parent is self.view_root:
            self.root_other = other

        rects = squarify(
            [node.size for node in layout_nodes],
            rect.x,
            rect.y,
            rect.w,
            rect.h,
        )
        normal: list[tuple[Node, Rect]] = []
        others: list[tuple[Node, Rect]] = []
        for node, child_rect in zip(layout_nodes, rects):
            if node.kind == "other":
                child_rect = self._expanded_rect(
                    child_rect,
                    rect,
                    OTHER_MIN_W,
                    OTHER_MIN_H,
                )
                others.append((node, child_rect))
            else:
                normal.append((node, child_rect))
        # Draw the synthetic Other box first so a normal sibling that it
        # overlaps stays visible and clickable; the toolbar Other button gives
        # reliable access to the aggregate.
        for node, child_rect in others:
            self._draw_node(node, child_rect, depth)
        for node, child_rect in normal:
            self._draw_node(node, child_rect, depth)

    @staticmethod
    def _is_tiny(rect: Rect) -> bool:
        width = rect.w - GAP
        height = rect.h - GAP
        if width < AGG_MIN_W or height < AGG_MIN_H:
            return True
        return width * height < AGG_MIN_AREA

    @staticmethod
    def _expanded_rect(
        rect: Rect,
        parent: Rect,
        min_width: float,
        min_height: float,
    ) -> Rect:
        """Give a synthetic Other box a compact, clickable minimum size."""
        min_rect_w = min_width + GAP
        min_rect_h = min_height + GAP
        if rect.w - GAP >= min_width and rect.h - GAP >= min_height:
            return rect

        area = max(rect.w * rect.h, min_rect_w * min_rect_h)
        aspect = (rect.w / rect.h) if rect.h > 0 else 1.0
        aspect = max(0.25, min(4.0, aspect))
        width = math.sqrt(area * aspect)
        height = area / width if width > 0 else min_rect_h
        if width < min_rect_w:
            width = min_rect_w
            height = area / width
        if height < min_rect_h:
            height = min_rect_h
            width = area / height
        width = min(max(width, min_rect_w), parent.w)
        height = min(max(height, min_rect_h), parent.h)

        center_x = rect.x + rect.w / 2
        center_y = rect.y + rect.h / 2
        x = center_x - width / 2
        y = center_y - height / 2
        max_x = max(parent.x, parent.x + parent.w - width)
        max_y = max(parent.y, parent.y + parent.h - height)
        x = min(max(parent.x, x), max_x)
        y = min(max(parent.y, y), max_y)
        return Rect(x, y, width, height)

    def _draw_node(self, node: Node, rect: Rect, depth: int) -> None:
        x0 = rect.x + GAP / 2
        y0 = rect.y + GAP / 2
        x1 = rect.x + rect.w - GAP / 2
        y1 = rect.y + rect.h - GAP / 2
        if x1 - x0 < 1 or y1 - y0 < 1:
            self._too_small += 1
            return

        if node.kind == "dir":
            can_nest = (
                depth < MAX_NEST_DEPTH
                and x1 - x0 >= MIN_FOLDER_W
                and y1 - y0
                >= self._header_h + MIN_FOLDER_CONTENT_H + 2 * FOLDER_CONTENT_PAD
            )
            if can_nest:
                self._draw_folder(node, x0, y0, x1, y1, depth)
            else:
                self._draw_leaf(
                    node,
                    x0,
                    y0,
                    x1,
                    y1,
                    COLOR_FOLDER_LEAF,
                    COLOR_FOLDER_OUTLINE,
                    COLOR_TEXT,
                    [sanitize_name(node.name), format_size(node.size)],
                )
        elif node.kind == "file":
            self._draw_leaf(
                node,
                x0,
                y0,
                x1,
                y1,
                COLOR_FILE,
                COLOR_FILE_OUTLINE,
                COLOR_TEXT,
                [sanitize_name(node.name), format_size(node.size)],
            )
        elif node.kind == "group":
            self._draw_leaf(
                node,
                x0,
                y0,
                x1,
                y1,
                COLOR_GROUP,
                COLOR_GROUP_OUTLINE,
                COLOR_TEXT,
                [
                    f"Small files ({format_count(node.small_count)})",
                    format_size(node.size),
                ],
            )
        elif node.kind == "other":
            self._draw_leaf(
                node,
                x0,
                y0,
                x1,
                y1,
                COLOR_OTHER,
                COLOR_OTHER_OUTLINE,
                COLOR_TEXT,
                ["Other", format_size(node.size)],
            )

    def _draw_folder(
        self,
        node: Node,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        depth: int,
    ) -> None:
        outer = self.create_rectangle(
            x0,
            y0,
            x1,
            y1,
            fill=COLOR_FOLDER_BODY,
            outline=COLOR_FOLDER_OUTLINE,
            width=1,
        )
        self._register_rect(node, outer, COLOR_FOLDER_OUTLINE, 1)

        header_bottom = y0 + self._header_h
        header = self.create_rectangle(
            x0,
            y0,
            x1,
            header_bottom,
            fill=COLOR_FOLDER_HEADER,
            outline=COLOR_FOLDER_OUTLINE,
            width=1,
        )
        self._item_nodes[header] = node

        max_text_w = (x1 - x0) - 2 * FOLDER_HEADER_PAD
        if max_text_w > 4:
            text = self._header_text(node, max_text_w)
            if text:
                text_id = self.create_text(
                    x0 + FOLDER_HEADER_PAD,
                    (y0 + header_bottom) / 2,
                    text=text,
                    anchor="w",
                    font=self.header_font,
                    fill=COLOR_HEADER_TEXT,
                )
                self._item_nodes[text_id] = node

        cx0 = x0 + FOLDER_CONTENT_PAD
        cy0 = header_bottom + FOLDER_CONTENT_PAD
        cx1 = x1 - FOLDER_CONTENT_PAD
        cy1 = y1 - FOLDER_CONTENT_PAD
        if cx1 - cx0 >= MIN_FOLDER_CONTENT_W and cy1 - cy0 >= MIN_FOLDER_CONTENT_H:
            self._layout_children(
                node,
                Rect(cx0, cy0, cx1 - cx0, cy1 - cy0),
                depth + 1,
            )

    def _header_text(self, node: Node, max_width: float) -> str:
        name = sanitize_name(node.name) or display_name(node.path)
        size = format_size(node.size)
        full = f"{name}  {size}"
        if self.header_font.measure(full) <= max_width:
            return full
        if self.header_font.measure(name) <= max_width:
            return name
        return truncate_text(self.header_font, name, max_width)

    def _draw_leaf(
        self,
        node: Node,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        fill: str,
        outline: str,
        text_color: str,
        lines: list[str],
    ) -> None:
        rect_id = self.create_rectangle(
            x0,
            y0,
            x1,
            y1,
            fill=fill,
            outline=outline,
            width=1,
        )
        self._register_rect(node, rect_id, outline, 1)
        self._draw_centered_text(
            node,
            x0,
            y0,
            x1,
            y1,
            lines,
            self.leaf_font,
            text_color,
        )

    def _draw_centered_text(
        self,
        node: Node,
        x0: float,
        y0: float,
        x1: float,
        y1: float,
        lines: list[str],
        font: tkfont.Font,
        color: str,
    ) -> None:
        avail_w = (x1 - x0) - 8
        avail_h = (y1 - y0) - 6
        line_h = font.metrics("linespace")
        if avail_w < 8 or avail_h < line_h or not lines:
            return
        name = truncate_text(font, lines[0], avail_w)
        if not name:
            return
        text_lines = [name]
        if len(lines) > 1 and avail_h >= 2 * line_h + 2:
            size_text = lines[1]
            if font.measure(size_text) <= avail_w:
                text_lines.append(size_text)
        text_id = self.create_text(
            (x0 + x1) / 2,
            (y0 + y1) / 2,
            text="\n".join(text_lines),
            font=font,
            fill=color,
            justify="center",
            anchor="center",
        )
        self._item_nodes[text_id] = node

    def _register_rect(
        self,
        node: Node,
        rect_id: int,
        outline: str,
        width: int,
    ) -> None:
        self._item_nodes[rect_id] = node
        self._primary_rects[id(node)] = rect_id
        self._rect_styles[rect_id] = (outline, width)

    def _reset_hover(self) -> None:
        if self._hovered_restore is not None:
            rect_id, outline, width = self._hovered_restore
            try:
                self.itemconfig(rect_id, outline=outline, width=width)
            except tk.TclError:
                pass
        self._hovered_restore = None
        self._hovered_node = None

    def _clear_hover(self, notify: bool = True) -> None:
        had_hover = self._hovered_node is not None
        self._reset_hover()
        try:
            self.configure(cursor="")
        except tk.TclError:
            pass
        if notify and had_hover:
            self.on_leave()

    def _on_motion(self, event: tk.Event) -> None:
        current = self.find_withtag("current")
        if not current:
            self._clear_hover()
            return
        node = self._item_nodes.get(current[0])
        if node is None:
            self._clear_hover()
            return
        if node is self._hovered_node:
            return
        self._reset_hover()
        self._hovered_node = node
        rect_id = self._primary_rects.get(id(node))
        if rect_id is not None:
            outline, width = self._rect_styles.get(rect_id, (COLOR_HOVER, 1))
            self._hovered_restore = (rect_id, outline, width)
            try:
                self.itemconfig(rect_id, outline=COLOR_HOVER, width=2)
            except tk.TclError:
                self._hovered_restore = None
        try:
            self.configure(
                cursor="hand2" if node.kind in ("dir", "group", "other") else ""
            )
        except tk.TclError:
            pass
        self.on_hover(node)

    def _on_leave(self, event: tk.Event | None = None) -> None:
        self._clear_hover()

    def _on_double_click(self, event: tk.Event) -> None:
        current = self.find_withtag("current")
        if not current:
            return
        node = self._item_nodes.get(current[0])
        if node is None:
            return
        if node.kind == "dir":
            self.on_zoom(node)
        elif node.kind == "group":
            self.on_group(node)
        elif node.kind == "other":
            self.on_other(node)

    def _on_right_click(self, event: tk.Event) -> None:
        current = self.find_withtag("current")
        if not current:
            return
        node = self._item_nodes.get(current[0])
        if node is None or node.kind in ("group", "other"):
            return
        if self._context_menu is not None:
            try:
                self._context_menu.destroy()
            except tk.TclError:
                pass
            self._context_menu = None
        menu = tk.Menu(self, tearoff=False)
        self._context_menu = menu

        def do_open() -> None:
            open_in_explorer(node.path)

        def do_copy() -> None:
            try:
                self.clipboard_clear()
                self.clipboard_append(strip_long_path_prefix(node.path))
            except tk.TclError:
                pass

        menu.add_command(label="Open in Explorer", command=do_open)
        menu.add_command(label="Copy path", command=do_copy)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.after(1, lambda: menu.destroy() if menu.winfo_exists() else None)
            self._context_menu = None

    def _on_button(self, event: tk.Event) -> None:
        self.focus_set()

    def _on_configure(self, event: tk.Event) -> None:
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
        self._resize_job = self.after(RESIZE_DEBOUNCE_MS, self._do_resize)

    def _do_resize(self) -> None:
        self._resize_job = None
        self.redraw()

    def _on_destroy(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._resize_job is not None:
            try:
                self.after_cancel(self._resize_job)
            except tk.TclError:
                pass
            self._resize_job = None


class ListWindow(tk.Toplevel):
    """A non-modal Treeview list used for small files and Other items."""

    def __init__(
        self,
        master: tk.Misc,
        *,
        title: str,
        header: str,
        rows: list[Row] | None = None,
        loader: Loader | None = None,
        show_path: bool = True,
        on_zoom: Callable[[Node], None] | None = None,
        on_group: Callable[[Node], None] | None = None,
        on_other: Callable[[Node], None] | None = None,
        on_close: Callable[["ListWindow"], None] | None = None,
    ) -> None:
        super().__init__(master)
        self.title(title)
        self.transient(master)
        self.minsize(560, 320)
        self._rows = rows
        self._loader = loader
        self._show_path = show_path
        self._on_zoom = on_zoom
        self._on_group = on_group
        self._on_other = on_other
        self._on_close = on_close
        self._nodes: dict[str, Node] = {}
        self._queue: queue.Queue = queue.Queue()
        self._cancel = threading.Event()
        self._closed = False
        self._poll_job: str | None = None

        frame = ttk.Frame(self, padding=(8, 8, 8, 8))
        frame.grid(row=0, column=0, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        self.header_label = ttk.Label(
            frame,
            text=header,
            justify="left",
            wraplength=820,
        )
        self.header_label.grid(row=0, column=0, sticky="ew", pady=(0, 6))

        tree_frame = ttk.Frame(frame)
        tree_frame.grid(row=1, column=0, sticky="nsew")
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        base_font = tkfont.nametofont("TkDefaultFont")
        row_height = max(20, base_font.metrics("linespace") + 8)
        ttk.Style(self).configure(
            "Treeview",
            font=base_font,
            rowheight=row_height,
        )

        if self._show_path:
            columns = ("name", "type", "size", "path")
        else:
            columns = ("name", "type", "size")
        self.tree = ttk.Treeview(
            tree_frame,
            columns=columns,
            show="headings",
            selectmode="browse",
        )
        self.tree.heading("name", text="Name")
        self.tree.heading("type", text="Type")
        self.tree.heading("size", text="Size")
        if self._show_path:
            self.tree.heading("path", text="Path")
        name_stretch = not self._show_path
        self.tree.column("name", width=230, anchor="w", stretch=name_stretch)
        self.tree.column("type", width=130, anchor="w", stretch=False)
        self.tree.column("size", width=130, anchor="e", stretch=False)
        if self._show_path:
            self.tree.column("path", width=380, anchor="w", stretch=True)
        self.tree.tag_configure("odd", background="#F7F7F7")
        self.tree.bind("<Double-1>", self._on_double_click)
        self.tree.bind("<Return>", self._on_double_click)

        yscroll = ttk.Scrollbar(
            tree_frame,
            orient="vertical",
            command=self.tree.yview,
        )
        xscroll = ttk.Scrollbar(
            tree_frame,
            orient="horizontal",
            command=self.tree.xview,
        )
        self.tree.configure(
            yscrollcommand=yscroll.set,
            xscrollcommand=xscroll.set,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")

        footer = ttk.Frame(frame)
        footer.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        footer.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(footer, text="", anchor="w")
        self.status_label.grid(row=0, column=0, sticky="ew")
        ttk.Button(footer, text="Close", command=self.close).grid(
            row=0, column=1, sticky="e"
        )

        self.protocol("WM_DELETE_WINDOW", self.close)
        self.bind("<Escape>", lambda event: self.close())
        self.bind("<Destroy>", self._on_destroy_event, add="+")

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        win_w = min(860, max(560, screen_w - 80))
        win_h = min(520, max(320, screen_h - 120))
        x = max(0, min(master.winfo_rootx() + 60, screen_w - win_w))
        y = max(0, min(master.winfo_rooty() + 60, screen_h - win_h))
        self.geometry(f"{win_w}x{win_h}+{x}+{y}")
        self.header_label.configure(wraplength=max(300, win_w - 40))
        self.bind("<Configure>", self._on_window_configure, add="+")

        if rows is not None:
            self._populate(rows, sum(row[2] for row in rows), 0)
        elif loader is not None:
            self.status_label.config(text="Scanning…")
            threading.Thread(target=self._loader_worker, daemon=True).start()
            self._poll_job = self.after(POLL_MS, self._poll_loader)
        else:
            self.status_label.config(text="No items.")

        self.lift()
        self.focus_set()

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        try:
            width = self.winfo_width()
        except tk.TclError:
            return
        if width > 1:
            self.header_label.configure(wraplength=max(300, width - 40))

    def _populate(self, rows: list[Row], total: int, errors: int) -> None:
        children = self.tree.get_children()
        if children:
            self.tree.delete(*children)
        self._nodes.clear()
        ordered = sorted(rows, key=lambda row: (-row[2], row[0].lower()))
        for index, (name, type_name, size, path, node) in enumerate(ordered):
            if self._show_path:
                values = (name, type_name, format_size(size), path)
            else:
                values = (name, type_name, format_size(size))
            item_id = self.tree.insert(
                "",
                "end",
                values=values,
                tags=("odd",) if index % 2 else (),
            )
            self._nodes[item_id] = node
        status = (
            f"{format_plural(len(ordered), 'item')} — "
            f"{format_size(total)} total"
        )
        if errors:
            status += f" — {format_plural(errors, 'unreadable item')}"
        self.status_label.config(text=status)

    def _loader_worker(self) -> None:
        try:
            result = self._loader(self._cancel) if self._loader is not None else None
            if self._cancel.is_set() or result is None:
                return
            self._queue.put(("done", result))
        except Exception as exc:
            if not self._cancel.is_set():
                self._queue.put(("error", str(exc)))

    def _poll_loader(self) -> None:
        self._poll_job = None
        if self._closed:
            return
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
        try:
            message = self._queue.get_nowait()
        except queue.Empty:
            try:
                self._poll_job = self.after(POLL_MS, self._poll_loader)
            except tk.TclError:
                self._poll_job = None
            return
        if message[0] == "done":
            rows, total, errors, error_message = message[1]
            if error_message:
                self.status_label.config(text=f"Could not read folder: {error_message}")
            else:
                self._populate(rows, total, errors)
        else:
            self.status_label.config(text=f"Could not read folder: {message[1]}")

    def _on_double_click(self, event: tk.Event) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        node = self._nodes.get(selection[0])
        if node is None:
            return
        if node.kind == "dir" and self._on_zoom is not None:
            self._on_zoom(node)
            self.close()
        elif node.kind == "group" and self._on_group is not None:
            self._on_group(node)
        elif node.kind == "other" and self._on_other is not None:
            self._on_other(node)

    def _cancel_poll(self) -> None:
        if self._poll_job is not None:
            try:
                self.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._cancel_poll()
        self._cancel.set()
        if self._on_close is not None:
            self._on_close(self)
        self.destroy()

    def _on_destroy_event(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self._closed:
            return
        self._closed = True
        self._cancel_poll()
        self._cancel.set()
        if self._on_close is not None:
            self._on_close(self)


class TreemapApp:
    """Top-level application: toolbar, breadcrumb, canvas and status bar."""

    def __init__(self, root: tk.Tk, initial_path: str | None = None) -> None:
        self.root = root
        self.initial_path = initial_path
        self.scanner = Scanner(LARGE_FILE_THRESHOLD)
        self.queue: queue.Queue = queue.Queue()
        self.scan_gen = 0
        self.cancel_event: threading.Event | None = None
        self.scanning = False
        self.scan_kind: str | None = None
        self.tree_root: Node | None = None
        self.view_node: Node | None = None
        self.nodes_by_path: dict[str, Node] = {}
        self.current_scan_root: str | None = None
        self._previous_state: (
            tuple[Node | None, dict[str, Node], str | None, Node | None, str] | None
        ) = None
        self._too_small_count = 0
        self._root_other: Node | None = None
        self._closing = False
        self._poll_job: str | None = None
        self._initial_job: str | None = None
        self.list_windows: list[ListWindow] = []

        self._build_ui()
        self._maximize()
        self._update_buttons()
        self._update_status_idle()
        self._update_summary()
        self._update_breadcrumb()
        self.view.show_message(
            "Choose a folder to analyse",
            "Use the path box above or click Browse…",
        )
        self.path_entry.focus_set()
        self._poll_job = self.root.after(POLL_MS, self._poll_queue)
        if self.initial_path:
            self._initial_job = self.root.after_idle(self._load_initial)

    def _build_ui(self) -> None:
        self.root.title(APP_TITLE)
        self.root.minsize(720, 480)
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(1100, max(720, screen_w - 80))
        height = min(720, max(480, screen_h - 120))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2)
        self.root.geometry(f"{width}x{height}+{x}+{y}")

        self.style = ttk.Style(self.root)
        self._breadcrumb_bg = (
            self.style.lookup("TFrame", "background") or self.root.cget("bg")
        )

        main = ttk.Frame(self.root, padding=(8, 8, 8, 4))
        main.grid(row=0, column=0, sticky="nsew")
        self.root.rowconfigure(0, weight=1)
        self.root.columnconfigure(0, weight=1)
        main.columnconfigure(0, weight=1)
        main.rowconfigure(2, weight=1)

        toolbar = ttk.Frame(main)
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(1, weight=1)
        ttk.Label(toolbar, text="Folder:").grid(
            row=0, column=0, padx=(0, 6), sticky="w"
        )
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(toolbar, textvariable=self.path_var)
        self.path_entry.grid(row=0, column=1, sticky="ew")
        self.path_entry.bind("<Return>", self._on_path_enter)
        self.browse_btn = ttk.Button(
            toolbar, text="Browse…", command=self.on_browse
        )
        self.browse_btn.grid(row=0, column=2, padx=(6, 0))
        self.up_btn = ttk.Button(toolbar, text="Up", command=self.on_up)
        self.up_btn.grid(row=0, column=3, padx=(6, 0))
        self.refresh_btn = ttk.Button(
            toolbar, text="Refresh", command=self.on_refresh
        )
        self.refresh_btn.grid(row=0, column=4, padx=(6, 0))
        self.other_btn = ttk.Button(
            toolbar, text="Other…", command=self.on_other_button
        )
        self.other_btn.grid(row=0, column=5, padx=(6, 0))

        breadcrumb_height = max(
            22, tkfont.nametofont("TkDefaultFont").metrics("linespace") + 6
        )
        self.breadcrumb_frame = tk.Frame(
            main,
            bg=self._breadcrumb_bg,
            width=1,
            height=breadcrumb_height,
        )
        self.breadcrumb_frame.grid(row=1, column=0, sticky="ew", pady=(6, 4))
        self.breadcrumb_frame.grid_propagate(False)

        separator = ttk.Separator(main, orient="horizontal")
        separator.grid(row=3, column=0, sticky="ew", pady=(6, 0))

        status_frame = ttk.Frame(main)
        status_frame.grid(row=4, column=0, sticky="ew")
        status_frame.columnconfigure(0, weight=1)
        self.status_label = ttk.Label(
            status_frame, text="", anchor="w", padding=(4, 3)
        )
        self.status_label.grid(row=0, column=0, sticky="ew")
        self.progress_label = ttk.Label(
            status_frame,
            text="",
            anchor="e",
            padding=(4, 3),
            foreground=COLOR_MUTED,
        )
        self.progress_label.grid(row=0, column=1, sticky="e")
        self.summary_label = ttk.Label(
            status_frame, text="", anchor="e", padding=(4, 3)
        )
        self.summary_label.grid(row=0, column=2, sticky="e")

        self.view = TreemapView(
            main,
            on_zoom=self._zoom_to,
            on_group=self._open_small_files,
            on_other=self._open_other,
            on_hover=self._on_hover,
            on_leave=self._on_leave,
            on_stats=self._on_view_stats,
        )
        self.view.grid(row=2, column=0, sticky="nsew")

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.root.bind("<F5>", lambda event: self.on_refresh())
        self.root.bind("<F11>", lambda event: self._toggle_fullscreen())
        self.root.bind("<Escape>", lambda event: self._on_escape())
        self.root.bind("<Alt-Up>", lambda event: self.on_up())
        self.root.bind("<Alt-Left>", lambda event: self.on_up())

    def _maximize(self) -> None:
        try:
            self.root.state("zoomed")
        except tk.TclError:
            try:
                self.root.attributes("-zoomed", True)
            except tk.TclError:
                pass

    def _is_fullscreen(self) -> bool:
        try:
            return bool(int(self.root.attributes("-fullscreen")))
        except (tk.TclError, TypeError, ValueError):
            return False

    def _set_fullscreen(self, enabled: bool) -> None:
        try:
            self.root.attributes("-fullscreen", enabled)
        except tk.TclError:
            pass

    def _toggle_fullscreen(self) -> None:
        self._set_fullscreen(not self._is_fullscreen())

    def _on_escape(self) -> None:
        if self.scanning:
            self._cancel_current_scan()
        elif self._is_fullscreen():
            self._set_fullscreen(False)

    def _load_initial(self) -> None:
        self._initial_job = None
        if self._closing or not self.initial_path:
            return
        self.load_path(self.initial_path)

    def _on_path_enter(self, event: tk.Event | None = None) -> None:
        self.load_path(self.path_var.get())

    def load_path(self, raw_path: str) -> bool:
        if self._closing:
            return False
        path = normalize_path(raw_path)
        if not path:
            messagebox.showinfo(APP_TITLE, "Enter a folder path.", parent=self.root)
            return False
        try:
            st = os.stat(path)
        except FileNotFoundError:
            messagebox.showerror(
                APP_TITLE,
                f"Folder not found:\n{raw_path}",
                parent=self.root,
            )
            return False
        except NotADirectoryError:
            messagebox.showerror(
                APP_TITLE,
                f"Not a folder:\n{raw_path}",
                parent=self.root,
            )
            return False
        except PermissionError:
            messagebox.showerror(
                APP_TITLE,
                f"Cannot access folder:\n{raw_path}",
                parent=self.root,
            )
            return False
        except OSError as exc:
            messagebox.showerror(
                APP_TITLE,
                f"Cannot access folder:\n{raw_path}\n\n{exc}",
                parent=self.root,
            )
            return False
        if not stat.S_ISDIR(st.st_mode):
            messagebox.showerror(
                APP_TITLE,
                f"Not a folder:\n{raw_path}",
                parent=self.root,
            )
            return False
        self._start_scan(path, "load")
        return True

    def on_browse(self) -> None:
        if self.view_node is not None and os.path.isdir(self.view_node.path):
            initial = self.view_node.path
        else:
            initial = os.path.expanduser("~")
        selected = filedialog.askdirectory(
            parent=self.root,
            initialdir=initial,
            mustexist=True,
            title="Choose a folder",
        )
        if selected:
            self.load_path(selected)

    def _start_scan(self, path: str, kind: str) -> None:
        if self._closing:
            return
        self._cancel_scan()
        self.scan_gen += 1
        generation = self.scan_gen
        cancel = threading.Event()
        self.cancel_event = cancel
        self.scanning = True
        self.scan_kind = kind
        self.progress_label.config(text="")

        if kind == "load":
            self._close_list_windows()
            if self.tree_root is not None:
                path_text = (
                    self.view_node.path if self.view_node else self.path_var.get()
                )
                self._previous_state = (
                    self.tree_root,
                    self.nodes_by_path,
                    self.current_scan_root,
                    self.view_node,
                    path_text,
                )
            self.tree_root = None
            self.nodes_by_path = {}
            self.current_scan_root = None
            self.view_node = None
            self._too_small_count = 0
            self.path_var.set(path)
            self.root.title(APP_TITLE)
            self.view.show_message("Scanning…", path)
            self._update_breadcrumb()
            self._update_summary()

        self._update_buttons()
        self._update_status_idle()
        threading.Thread(
            target=self._scan_worker,
            args=(path, generation, cancel),
            daemon=True,
        ).start()

    def _scan_worker(
        self,
        path: str,
        generation: int,
        cancel: threading.Event,
    ) -> None:
        def progress(current_path: str, stats: ScanStats) -> None:
            self.queue.put(
                ("progress", generation, stats.files, stats.bytes)
            )

        try:
            result = self.scanner.scan(path, cancel, progress)
            if cancel.is_set():
                self.queue.put(("cancelled", generation))
            else:
                self.queue.put(("done", generation, result))
        except ScanCancelled:
            self.queue.put(("cancelled", generation))
        except Exception as exc:
            self.queue.put(
                ("error", generation, f"{type(exc).__name__}: {exc}")
            )

    def _poll_queue(self) -> None:
        self._poll_job = None
        if self._closing:
            return
        while True:
            try:
                message = self.queue.get_nowait()
            except queue.Empty:
                break
            if message[0] == "progress":
                _, generation, files, byte_count = message
                if generation == self.scan_gen and self.scanning:
                    self.progress_label.config(
                        text=(
                            f"{format_plural(files, 'file')}, "
                            f"{format_size(byte_count)} scanned"
                        )
                    )
            elif message[0] == "done":
                _, generation, result = message
                if generation == self.scan_gen:
                    self._handle_done(result)
            elif message[0] == "cancelled":
                _, generation = message
                if generation == self.scan_gen:
                    self._handle_cancelled()
            elif message[0] == "error":
                _, generation, error = message
                if generation == self.scan_gen:
                    self._handle_error(error)
        if not self._closing:
            try:
                self._poll_job = self.root.after(POLL_MS, self._poll_queue)
            except tk.TclError:
                self._poll_job = None

    def _handle_done(self, result: ScanResult) -> None:
        kind = self.scan_kind
        if (
            result.root.scan_failed
            and result.root.size == 0
            and not result.root.children
        ):
            self._handle_error("The folder could not be read.")
            return
        target_path = self.view_node.path if self.view_node else None
        if kind == "refresh":
            self._close_list_windows()

        self.tree_root = result.root
        self.nodes_by_path = result.nodes_by_path
        self.current_scan_root = result.root.path

        fallback = False
        if kind == "refresh" and target_path:
            new_view = self.nodes_by_path.get(path_key(target_path))
            if new_view is None:
                fallback = True
                new_view = result.root
            self.view_node = new_view
        else:
            self.view_node = result.root

        self.scanning = False
        self.cancel_event = None
        self.scan_kind = None
        self.progress_label.config(text="")
        self._previous_state = None

        self.path_var.set(self.view_node.path)
        self.root.title(f"du – {self.view_node.name}")
        self.view.set_root(self.view_node)
        self._update_breadcrumb()
        self._update_buttons()
        self._update_summary()

        if kind == "refresh":
            status = f"Refreshed in {result.elapsed:.1f} s"
            if fallback:
                status += " — previous folder no longer exists"
        else:
            status = f"Scanned in {result.elapsed:.1f} s"
        self.status_label.config(text=status)

    def _handle_cancelled(self) -> None:
        kind = self.scan_kind
        self.scanning = False
        self.cancel_event = None
        self.scan_kind = None
        self.progress_label.config(text="")
        if kind == "load":
            self._restore_previous_state()
            self.status_label.config(text="Scan cancelled")
        else:
            self._update_buttons()
            self._update_status_idle()
            self.status_label.config(text="Refresh cancelled")

    def _handle_error(self, error: str) -> None:
        kind = self.scan_kind
        self.scanning = False
        self.cancel_event = None
        self.scan_kind = None
        self.progress_label.config(text="")
        if kind == "load":
            self._restore_previous_state()
        else:
            self._update_buttons()
            self._update_status_idle()
        self.status_label.config(text="Scan failed")
        messagebox.showerror(
            APP_TITLE,
            f"Could not scan:\n{error}",
            parent=self.root,
        )

    def _restore_previous_state(self) -> None:
        state = self._previous_state
        self._previous_state = None
        if state is None:
            self.tree_root = None
            self.nodes_by_path = {}
            self.current_scan_root = None
            self.view_node = None
            self._too_small_count = 0
            self.path_var.set("")
            self.root.title(APP_TITLE)
            self.view.show_message(
                "Choose a folder to analyse",
                "Use the path box above or click Browse…",
            )
            self._update_breadcrumb()
            self._update_summary()
            self._update_buttons()
            self._update_status_idle()
            return

        (
            tree_root,
            nodes_by_path,
            current_scan_root,
            view_node,
            path_text,
        ) = state
        self.tree_root = tree_root
        self.nodes_by_path = nodes_by_path
        self.current_scan_root = current_scan_root
        self.view_node = view_node
        self.path_var.set(path_text or (view_node.path if view_node else ""))
        if view_node is not None:
            self.root.title(f"du – {view_node.name}")
            self.view.set_root(view_node)
        else:
            self.root.title(APP_TITLE)
            self.view.show_message(
                "Choose a folder to analyse",
                "Use the path box above or click Browse…",
            )
        self._update_breadcrumb()
        self._update_summary()
        self._update_buttons()
        self._update_status_idle()

    def _cancel_scan(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
        self.cancel_event = None
        self.scanning = False

    def _cancel_current_scan(self) -> None:
        if self.scanning and self.cancel_event is not None:
            self.cancel_event.set()
            self.status_label.config(text="Cancelling…")

    def on_other_button(self) -> None:
        if self._root_other is not None:
            self._open_other(self._root_other)

    def on_refresh(self) -> None:
        if not self.tree_root or not self.view_node or self.scanning:
            return
        self._start_scan(self.tree_root.path, "refresh")

    def on_up(self) -> None:
        if self.view_node is None:
            return
        if self.view_node.parent is not None:
            self._zoom_to(self.view_node.parent)

    def _zoom_to(self, node: Node) -> None:
        if node.kind != "dir":
            return
        current = self.nodes_by_path.get(path_key(node.path))
        if current is None:
            return
        node = current
        if node is self.view_node:
            return
        self.view_node = node
        self.path_var.set(node.path)
        self.root.title(f"du – {node.name}")
        self.view.set_root(node)
        self._update_breadcrumb()
        self._update_buttons()
        self._update_summary()
        self._update_status_idle()

    def _on_breadcrumb_click(self, path: str) -> None:
        if self.tree_root is None:
            return
        if not is_within_path(path, self.tree_root.path):
            return
        node = self.nodes_by_path.get(path_key(path))
        if node is not None:
            self._zoom_to(node)

    def _update_breadcrumb(self) -> None:
        for child in self.breadcrumb_frame.winfo_children():
            child.destroy()
        if self.view_node is None:
            return

        path = strip_long_path_prefix(self.view_node.path)
        path_type = PureWindowsPath if is_windows() else PurePosixPath
        try:
            parsed = path_type(path)
        except Exception:
            return

        segments: list[tuple[str, str]] = []
        anchor = parsed.anchor
        if anchor:
            display = anchor.rstrip("\\/") or anchor
            segments.append((display, clean_path(anchor)))
        current = path_type(anchor) if anchor else path_type()
        start_index = 1 if anchor else 0
        for part in parsed.parts[start_index:]:
            current = current / part
            segments.append((part, clean_path(str(current))))
        if not segments:
            segments.append((path, path))

        # Pack from the current segment backwards so the most useful part of
        # the breadcrumb stays visible when the path is wider than the window.
        scan_root = self.tree_root.path if self.tree_root is not None else None
        for index in range(len(segments) - 1, -1, -1):
            label, segment_path = segments[index]
            is_current = index == len(segments) - 1
            within_root = (
                scan_root is not None
                and is_within_path(segment_path, scan_root)
            )
            clickable = within_root and not is_current
            widget = tk.Label(
                self.breadcrumb_frame,
                text=label,
                bg=self._breadcrumb_bg,
                fg=(
                    COLOR_TEXT
                    if is_current
                    else (COLOR_LINK if clickable else COLOR_MUTED)
                ),
                font=self.view.bold_font if is_current else self.view.ui_font,
                cursor="hand2" if clickable else "",
            )
            widget.pack(side="right", padx=2)
            if clickable:
                widget.bind(
                    "<Button-1>",
                    lambda event, segment=segment_path: self._on_breadcrumb_click(
                        segment
                    ),
                )
            if index > 0:
                tk.Label(
                    self.breadcrumb_frame,
                    text="›",
                    bg=self._breadcrumb_bg,
                    fg=COLOR_MUTED,
                ).pack(side="right")

    def _update_buttons(self) -> None:
        has_tree = self.tree_root is not None and self.view_node is not None
        up_ok = (
            has_tree
            and self.view_node is not None
            and self.view_node.parent is not None
        )
        self.up_btn.state(["!disabled"] if up_ok else ["disabled"])
        refresh_ok = has_tree and not self.scanning
        self.refresh_btn.state(["!disabled"] if refresh_ok else ["disabled"])
        self._update_other_button()

    def _update_summary(self) -> None:
        node = self.view_node
        if node is None:
            self.summary_label.config(text="")
            return
        parts = [
            format_size(node.size),
            format_plural(node.file_count, "file"),
            format_plural(node.dir_count, "folder"),
        ]
        empty_count = node.empty_file_count + node.empty_dir_count
        if empty_count:
            parts.append(f"{format_plural(empty_count, 'empty item')} hidden")
        if node.link_count:
            parts.append(f"{format_plural(node.link_count, 'link')} skipped")
        if node.error_count:
            parts.append(format_plural(node.error_count, "unreadable item"))
        if self._too_small_count:
            parts.append(
                format_plural(
                    self._too_small_count,
                    "item not drawn",
                    "items not drawn",
                )
            )
        self.summary_label.config(text="  •  ".join(parts))

    def _on_view_stats(self, too_small: int) -> None:
        self._too_small_count = too_small
        self._root_other = self.view.root_other
        self._update_other_button()
        self._update_summary()

    def _update_other_button(self) -> None:
        enabled = self.view_node is not None and self._root_other is not None
        self.other_btn.state(["!disabled"] if enabled else ["disabled"])

    def _on_hover(self, node: Node) -> None:
        percentage = ""
        if node.parent is not None and node.parent.size > 0:
            percentage = (
                f"  •  {node.size / node.parent.size * 100:.1f}% "
                f"of {node.parent.name}"
            )
        if node.kind == "dir":
            detail = (
                f"{format_plural(node.file_count, 'file')}, "
                f"{format_plural(node.dir_count, 'folder')}"
            )
        elif node.kind == "file":
            detail = "large file"
        elif node.kind == "group":
            detail = format_plural(node.small_count, "small file")
        else:
            detail = format_plural(len(node.children), "aggregated item")
        self.status_label.config(
            text=f"{node.path}  •  {format_size(node.size)}  •  {detail}{percentage}"
        )

    def _on_leave(self) -> None:
        self._update_status_idle()

    def _update_status_idle(self) -> None:
        if self.scanning:
            verb = "Refreshing…" if self.scan_kind == "refresh" else "Scanning…"
            text = f"{verb}  (Esc to cancel)"
        elif self.tree_root is not None and self.view_node is not None:
            text = "Ready — double-click a folder box to zoom; hover for details."
        else:
            text = "No folder loaded — choose a folder to begin."
        self.status_label.config(text=text)

    def _open_small_files(self, node: Node) -> None:
        if node.kind != "group":
            return
        folder_path = node.path
        folder_name = display_name(folder_path)
        title = f"Small files — {truncate_middle(folder_name, 40)}"
        header = (
            f"Small files in {truncate_middle(folder_path, 80)}\n"
            f"{format_plural(node.small_count, 'file')}  •  "
            f"{format_size(node.size)} total  •  "
            f"under {format_size(LARGE_FILE_THRESHOLD)}"
        )
        loader = lambda cancel: load_small_files(  # noqa: E731
            folder_path, cancel, LARGE_FILE_THRESHOLD
        )
        window = ListWindow(
            self.root,
            title=title,
            header=header,
            loader=loader,
            show_path=False,
            on_close=self._forget_list_window,
        )
        self.list_windows.append(window)

    def _open_other(self, node: Node) -> None:
        if node.kind != "other":
            return
        parent_name = (
            node.parent.name if node.parent is not None else display_name(node.path)
        )
        rows: list[Row] = []
        for child in sorted(
            node.children, key=lambda item: (-item.size, item.name.lower())
        ):
            if child.kind == "dir":
                type_name = "Folder"
            elif child.kind == "file":
                type_name = "Large file"
            elif child.kind == "group":
                type_name = "Small files"
            else:
                type_name = child.kind
            rows.append((child.name, type_name, child.size, child.path, child))
        total = sum(child.size for child in node.children)
        title = f"Other — {truncate_middle(parent_name, 40)}"
        header = (
            f"Items aggregated into Other in {truncate_middle(node.path, 80)}\n"
            f"{format_plural(len(rows), 'item')}  •  {format_size(total)} total"
        )
        window = ListWindow(
            self.root,
            title=title,
            header=header,
            rows=rows,
            on_zoom=self._zoom_to,
            on_group=self._open_small_files,
            on_other=self._open_other,
            on_close=self._forget_list_window,
        )
        self.list_windows.append(window)

    def _forget_list_window(self, window: ListWindow) -> None:
        try:
            self.list_windows.remove(window)
        except ValueError:
            pass

    def _close_list_windows(self) -> None:
        for window in list(self.list_windows):
            try:
                window.close()
            except tk.TclError:
                pass
        self.list_windows.clear()

    def on_close(self) -> None:
        self._closing = True
        if self._poll_job is not None:
            try:
                self.root.after_cancel(self._poll_job)
            except tk.TclError:
                pass
            self._poll_job = None
        if self._initial_job is not None:
            try:
                self.root.after_cancel(self._initial_job)
            except tk.TclError:
                pass
            self._initial_job = None
        self._cancel_scan()
        self._close_list_windows()
        try:
            self.root.destroy()
        except tk.TclError:
            pass


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print("Usage: python du.py [folder]")
        return
    enable_dpi_awareness()
    root = tk.Tk()
    initial_path = sys.argv[1] if len(sys.argv) > 1 else None
    TreemapApp(root, initial_path)
    root.mainloop()


if __name__ == "__main__":
    main()
