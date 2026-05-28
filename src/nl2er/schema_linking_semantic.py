from __future__ import annotations

from pathlib import Path
from typing import Any

from src.er2data.schema_utils import read_json_object
from src.nl2er.table_semantic_sketch import (
    collect_evidence_columns_from_unit,
    normalize_model_profile,
    unique_nonempty_strings,
)


DEFAULT_NL2ER_OUTPUT_FILENAME = "nl2er_output.json"
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_SAMPLE_ROW_LIMIT = 5
DEFAULT_SAMPLE_VALUE_MAX_CHARS = 200
MAX_TABLE_SKETCH_CONCURRENCY = 64
DEFAULT_TABLE_SKETCH_CONCURRENCY = MAX_TABLE_SKETCH_CONCURRENCY


def normalize_name_filters(values: list[str] | None) -> set[str]:
    filters: set[str] = set()
    for value in values or []:
        for item in str(value or "").split(","):
            text = item.strip()
            if text:
                filters.add(text)
    return filters


def read_sub_questions(
    *,
    case_dir: Path,
    nl2er_output_path: str | Path | None,
    nl2er_output_filename: str,
) -> str:
    path = Path(nl2er_output_path) if nl2er_output_path is not None else case_dir / nl2er_output_filename
    if not path.exists():
        return ""
    try:
        payload = read_json_object(path)
    except Exception:
        return ""
    resolve_process = payload.get("resolve_process")
    if isinstance(resolve_process, list):
        return "\n".join(str(item or "").strip() for item in resolve_process if str(item or "").strip())
    sub_questions = payload.get("sub_questions")
    if isinstance(sub_questions, list):
        return "\n".join(str(item or "").strip() for item in sub_questions if str(item or "").strip())
    return str(sub_questions or "").strip()


def resolve_table_sketch_concurrency(requested_concurrency: int | None) -> int:
    requested = requested_concurrency or DEFAULT_TABLE_SKETCH_CONCURRENCY
    return max(1, min(int(requested), MAX_TABLE_SKETCH_CONCURRENCY))


def sanitize_table_snapshot(
    table: dict[str, Any],
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> dict[str, Any]:
    sanitized = dict(table)
    sanitized["sample_rows"] = [
        {
            key: str(value)[:sample_value_max_chars]
            if value is not None and not isinstance(value, (int, float, bool))
            else value
            for key, value in row.items()
        }
        for row in sanitized.get("sample_rows", [])[:sample_row_limit]
        if isinstance(row, dict)
    ]
    columns: list[dict[str, Any]] = []
    for column in sanitized.get("columns", []):
        if not isinstance(column, dict):
            continue
        item = dict(column)
        item["sample_values"] = [
            str(value)[:sample_value_max_chars]
            if value is not None and not isinstance(value, (int, float, bool))
            else value
            for value in item.get("sample_values", [])[:sample_values_per_column]
        ]
        columns.append(item)
    sanitized["columns"] = columns
    return sanitized


def build_schema_linking_with_overall_result(
    *,
    schema_linking_payload: dict[str, Any],
    context: dict[str, Any],
    linked_tables: list[str],
    linked_columns: list[str],
    source: str,
) -> dict[str, Any]:
    payload = dict(schema_linking_payload)
    payload["question"] = {
        **(payload.get("question") if isinstance(payload.get("question"), dict) else {}),
        "unit_name": "question",
        "unit_type": "question",
        "question_id": str(context.get("question_id") or ""),
        "db_id": str(context.get("db_id") or ""),
        "question": str(context.get("user_intent") or context.get("question") or ""),
        "sub_questions": str(context.get("sub_questions") or ""),
        "linked_tables": unique_nonempty_strings(linked_tables),
        "linked_columns": unique_nonempty_strings(linked_columns),
        "schema_linking": {
            "ok": True,
            "source": source,
            "table_count": len(unique_nonempty_strings(linked_tables)),
            "column_count": len(unique_nonempty_strings(linked_columns)),
        },
    }
    return payload
