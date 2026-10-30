"""Smoke tests for du.py (standard library only).

Run with: py -3 smoke_test.py
"""
import os
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import du


def make_fixture(root: str) -> None:
    os.makedirs(os.path.join(root, "sub", "nested"), exist_ok=True)
    with open(os.path.join(root, "a.txt"), "w") as f:
        f.write("x" * 5)
    with open(os.path.join(root, "sub", "b.txt"), "w") as f:
        f.write("y" * 10)
    open(os.path.join(root, "empty.txt"), "w").close()
    os.makedirs(os.path.join(root, "emptydir"), exist_ok=True)


def test_scanner() -> None:
    with tempfile.TemporaryDirectory() as d:
        make_fixture(d)
        cancel = threading.Event()
        result = du.Scanner(du.LARGE_FILE_THRESHOLD).scan(d, cancel, lambda p, s: None)
        assert result.root.file_count == 3, result.root.file_count
        assert result.root.dir_count == 3, result.root.dir_count
        assert result.root.empty_file_count == 1, result.root.empty_file_count
        assert result.root.empty_dir_count == 2, result.root.empty_dir_count
        assert result.root.size == 15, result.root.size
        assert len(result.nodes_by_path) == 4  # root, sub, nested, emptydir
        # children include the small-files group
        kinds = {c.kind for c in result.root.children}
        assert "group" in kinds


def test_squarify_counts() -> None:
    for sizes in ([], [10], [5, 5, 1, 1], [100, 1, 1, 1, 1, 1, 1, 1, 1, 1]):
        rects = du.squarify(sizes, 0, 0, 200, 150)
        assert len(rects) == len(sizes)


def test_format_size() -> None:
    assert du.format_size(0) == "0 B"
    assert du.format_size(1024) == "1.00 KiB"
    assert du.format_size(100 * 1024 * 1024) == "100 MiB"


def test_paths() -> None:
    assert du.is_within_path("C:\\a\\b", "C:\\a")
    assert not du.is_within_path("C:\\x", "C:\\a")
    assert du.strip_long_path_prefix("\\\\?\\C:\\foo") == "C:\\foo"


def main() -> int:
    tests = [test_scanner, test_squarify_counts, test_format_size, test_paths]
    for t in tests:
        t()
        print(f"ok: {t.__name__}")
    print("ALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())