import json
from pathlib import Path

from karox6.core import snapshot
from karox6.core.snapshot import write_snapshot


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "pkg").mkdir(parents=True)
    (ws / "pkg" / "a.py").write_text("print(1)\n", encoding="utf-8")
    (ws / "b.txt").write_text("b\n", encoding="utf-8")
    return ws


def test_snapshot_copies_files_and_records_digests(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    target = write_snapshot(
        tmp_path / "s", ws, ["pkg/a.py", "b.txt", "gone.py"], step=4, status="completed"
    )
    assert target.name == "01"
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["index"] == 1
    assert manifest["step"] == 4
    assert manifest["status"] == "completed"
    assert manifest["files"]["gone.py"] is None
    assert len(manifest["files"]["pkg/a.py"]) == 64
    assert (target / "files" / "pkg" / "a.py").read_text(encoding="utf-8") == "print(1)\n"
    assert "skipped" not in manifest


def test_snapshot_indexes_increase(tmp_path: Path) -> None:
    ws = _ws(tmp_path)
    first = write_snapshot(tmp_path / "s", ws, ["b.txt"], step=1, status="completed")
    second = write_snapshot(tmp_path / "s", ws, [], step=2, status="limit")
    assert (first.name, second.name) == ("01", "02")
    manifest = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == {}
    assert manifest["index"] == 2


def test_snapshot_skips_outside_and_large_files(tmp_path: Path, monkeypatch) -> None:
    ws = _ws(tmp_path)
    monkeypatch.setattr(snapshot, "MAX_FILE_BYTES", 3)
    target = write_snapshot(
        tmp_path / "s",
        ws,
        ["b.txt", "pkg/a.py", "../x.py", "/etc/passwd", "pkg"],
        step=1,
        status="x",
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"] == {"b.txt": manifest["files"]["b.txt"]}
    assert sorted(manifest["skipped"]) == sorted(["pkg/a.py", "../x.py", "/etc/passwd", "pkg"])
    assert not (target / "files" / "pkg").exists()
