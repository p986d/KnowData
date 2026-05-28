from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from src.nl2sql.defaults import DEFAULT_REFORCE_ROOT, DEFAULT_SPIDER2_ROOT


DEFAULT_SAMPLE_VALUES_PER_COLUMN = 5
_SAFE_STEM_RE = re.compile(r"[^A-Za-z0-9_.-]+")


def resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    return resolved


def read_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object at {file_path}, got {type(payload).__name__}."
        )
    return payload


def read_sidecar_context(case_dir: str | Path) -> dict[str, str]:
    directory = Path(case_dir)
    context = {
        "question_id": "",
        "user_intent": "",
        "db_id": "",
        "db_hint": "",
        "external_knowledge": "",
    }
    for filename in ("input.json", "nl2er_input.json", "run_context.json"):
        candidate_path = directory / filename
        if not candidate_path.exists():
            continue
        try:
            payload = read_json_object(candidate_path)
        except Exception:
            continue
        if not context["question_id"]:
            context["question_id"] = str(
                payload.get("question_id") or payload.get("instance_id") or ""
            ).strip()
        if not context["user_intent"]:
            context["user_intent"] = str(
                payload.get("user_intent") or payload.get("instruction") or ""
            ).strip()
        if not context["db_id"]:
            context["db_id"] = str(payload.get("db_id") or "").strip()
        if not context["db_hint"]:
            context["db_hint"] = str(payload.get("db_hint") or "").strip()
        if not context["external_knowledge"]:
            external_knowledge = payload.get("external_knowledge")
            if isinstance(external_knowledge, str):
                context["external_knowledge"] = external_knowledge.strip()
    return context


def ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def split_qualified_name(value: str) -> list[str]:
    return [part.strip().strip('"').strip("`") for part in str(value or "").split(".") if part.strip()]


def table_name_from_fullname(value: str) -> str:
    parts = split_qualified_name(value)
    return parts[-1] if parts else ""


def table_fullname_from_column(value: str) -> str:
    parts = split_qualified_name(value)
    if len(parts) <= 1:
        return ""
    return ".".join(parts[:-1])


def column_name_from_fullname(value: str) -> str:
    parts = split_qualified_name(value)
    return parts[-1] if parts else ""


def resolve_snapshot_search_roots(
    *,
    db_id: str,
    database_root: str | Path | None = None,
    spider2_root: str | Path | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    if database_root is not None:
        root = resolve_path(database_root)
        candidates.extend([root, root / db_id])

    spider_candidates: list[Path] = []
    if spider2_root is not None:
        spider_candidates.append(resolve_path(spider2_root))
    spider_candidates.extend(
        [
            DEFAULT_REFORCE_ROOT / "spider2-snow",
            DEFAULT_SPIDER2_ROOT,
        ]
    )

    for root in spider_candidates:
        candidates.extend(
            [
                root / "resource" / "databases" / db_id,
                root / "databases" / db_id,
            ]
        )

    resolved: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        path = resolve_path(candidate)
        key = str(path).casefold()
        if key in seen or not path.exists():
            continue
        resolved.append(path)
        seen.add(key)
    return resolved


def sample_column_values(
    sample_rows: Any,
    column_name: str,
    *,
    limit: int,
) -> list[Any]:
    if not isinstance(sample_rows, list):
        return []
    values: list[Any] = []
    seen: set[str] = set()
    for row in sample_rows:
        if not isinstance(row, dict) or column_name not in row:
            continue
        value = row.get(column_name)
        key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen:
            continue
        values.append(value)
        seen.add(key)
        if len(values) >= limit:
            break
    return values


def safe_file_stem(value: str) -> str:
    text = _SAFE_STEM_RE.sub("_", str(value or "").strip()).strip("._")
    return text or "item"
