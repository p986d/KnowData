from __future__ import annotations

from pathlib import Path


def test_nl2er_and_er2data_do_not_import_run_modules():
    src_root = Path("src")
    checked_files = [
        *Path("src/nl2er").rglob("*.py"),
        *Path("src/er2data").rglob("*.py"),
    ]

    offenders: list[str] = []
    for path in checked_files:
        source = path.read_text(encoding="utf-8")
        if "from src.run" in source or "import src.run" in source:
            offenders.append(str(path.relative_to(src_root.parent)))

    assert offenders == []
