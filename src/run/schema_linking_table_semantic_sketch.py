from __future__ import annotations

import argparse
import json
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.prompt.prompt_builder import PromptBuilder
from src.run.nl2er_refine_0 import (
    DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    build_schema_evidence,
    ensure_dict_list,
    normalize_schema_linking_payload,
    read_sidecar_context,
    resolve_path,
    safe_file_stem,
    table_fullname_from_column,
)
from src.run.nl2er_only import positive_int
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


DEFAULT_METADATA_DIR = Path("metadata")
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/table_semantic_sketch")
DEFAULT_SCHEMA_LINKING_FILENAME = "schema_linking.json"
DEFAULT_NL2ER_OUTPUT_FILENAME = "nl2er_output.json"
DEFAULT_OUTPUT_FILENAME = "table_semantic_sketch.json"
DEFAULT_SAMPLE_ROW_LIMIT = 2
DEFAULT_SAMPLE_VALUE_MAX_CHARS = 300
MAX_TABLE_SKETCH_CONCURRENCY = 64
DEFAULT_TABLE_SKETCH_CONCURRENCY = MAX_TABLE_SKETCH_CONCURRENCY
BINARY_SAMPLE_PLACEHOLDER = "bytearray(b'...')"
TEMPLATE_KEY = "schema_linking_table_semantic_sketch"
TEMPLATE_NAME = "SchemaLinking_table_semantic_sketch.md"


@dataclass(slots=True)
class CaseInput:
    case_dir: Path
    relative_case_dir: Path
    schema_linking_path: Path
    output_path: Path


@dataclass(slots=True)
class PreparedCase:
    case_input: CaseInput
    runner: TableSemanticSketchRunner
    context: dict[str, str]
    schema_linking_payload: dict[str, Any]
    table_payloads: list[dict[str, Any]]
    started_at: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "For each linked table in a schema-linking file, sketch the logical "
            "entity/relationship/attribute semantics that the table can support "
            "for the question."
        )
    )
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--schema-linking-path", type=Path, default=None)
    parser.add_argument("--nl2er-output-path", type=Path, default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--schema-linking-filename", default=DEFAULT_SCHEMA_LINKING_FILENAME)
    parser.add_argument("--nl2er-output-filename", default=DEFAULT_NL2ER_OUTPUT_FILENAME)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--question", default=None)
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run when --metadata-dir is used. Supports comma-separated values.",
    )
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--db-hint", default="")
    parser.add_argument("--external-knowledge", default="")
    parser.add_argument("--table-fullname", nargs="+", default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument("--database-root", type=Path, default=None)
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument(
        "--sample-values-per-column",
        type=positive_int,
        default=DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    )
    parser.add_argument("--sample-row-limit", type=int, default=DEFAULT_SAMPLE_ROW_LIMIT)
    parser.add_argument(
        "--sample-value-max-chars",
        type=int,
        default=DEFAULT_SAMPLE_VALUE_MAX_CHARS,
    )
    parser.add_argument(
        "--max-workers",
        type=positive_int,
        default=None,
        help="Deprecated alias kept for compatibility; use --max-table-concurrency.",
    )
    parser.add_argument(
        "--max-table-concurrency",
        type=positive_int,
        default=None,
        help=(
            "Maximum parallel table sketch LLM requests across all questions/cases. "
            f"Defaults to {DEFAULT_TABLE_SKETCH_CONCURRENCY} and is capped at "
            f"{MAX_TABLE_SKETCH_CONCURRENCY}."
        ),
    )
    parser.add_argument(
        "--write-enriched-schema-linking",
        action="store_true",
        help=(
            "Write the question-level enriched schema linking result back to the "
            "input schema-linking file."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def read_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object at {file_path}, got {type(payload).__name__}."
        )
    return payload


def normalize_table_filters(values: list[str] | None) -> set[str]:
    filters: set[str] = set()
    for value in values or []:
        for item in str(value or "").split(","):
            text = item.strip()
            if text:
                filters.add(text.casefold())
    return filters


def normalize_name_filters(values: list[str] | None) -> set[str]:
    filters: set[str] = set()
    for value in values or []:
        for item in str(value or "").split(","):
            text = item.strip()
            if text:
                filters.add(text)
    return filters


def filter_cases_by_question_id(
    *,
    cases: list[CaseInput],
    requested_question_ids: set[str],
) -> list[CaseInput]:
    if not requested_question_ids:
        return cases

    requested = {question_id.casefold() for question_id in requested_question_ids}
    selected: list[CaseInput] = []
    seen: set[str] = set()
    for case in cases:
        context = read_sidecar_context(case.case_dir)
        question_id = str(context.get("question_id") or "").strip()
        if not question_id:
            try:
                payload = read_json_object(case.schema_linking_path)
                question_payload = payload.get("question")
                if isinstance(question_payload, dict):
                    question_id = str(question_payload.get("question_id") or "").strip()
            except Exception:
                question_id = ""
        if not question_id:
            question_id = case.case_dir.name.split("_", 1)[0]
        if question_id.casefold() in requested:
            selected.append(case)
            seen.add(question_id.casefold())

    missing = [
        question_id
        for question_id in requested_question_ids
        if question_id.casefold() not in seen
    ]
    if missing:
        raise ValueError(
            "The following question_id values were not found under metadata-dir: "
            + ", ".join(sorted(missing))
        )
    return selected


def format_sub_questions(value: Any) -> str:
    if isinstance(value, list):
        return "\n".join(
            text
            for text in (str(item or "").strip() for item in value)
            if text
        )
    if isinstance(value, str):
        return value.strip()
    return ""


def read_sub_questions(
    *,
    case_dir: Path,
    nl2er_output_path: str | Path | None = None,
    nl2er_output_filename: str = DEFAULT_NL2ER_OUTPUT_FILENAME,
) -> str:
    candidate_path = (
        resolve_path(nl2er_output_path)
        if nl2er_output_path is not None
        else case_dir / nl2er_output_filename
    )
    if not candidate_path.exists():
        return ""
    try:
        payload = read_json_object(candidate_path)
    except Exception:
        return ""
    return format_sub_questions(payload.get("resolve_process"))


def discover_case_inputs(
    *,
    metadata_dir: str | Path | None,
    schema_linking_path: str | Path | None,
    output_path: str | Path | None,
    schema_linking_filename: str,
    output_filename: str,
) -> tuple[Path, list[CaseInput]]:
    if schema_linking_path is not None:
        resolved_schema_linking_path = resolve_path(schema_linking_path)
        if not resolved_schema_linking_path.exists():
            raise FileNotFoundError(
                f"Schema linking file does not exist: {resolved_schema_linking_path}"
            )
        resolved_output_path = (
            resolve_path(output_path)
            if output_path is not None
            else resolved_schema_linking_path.with_name(output_filename)
        )
        case_dir = resolved_schema_linking_path.parent
        return case_dir, [
            CaseInput(
                case_dir=case_dir,
                relative_case_dir=Path(case_dir.name),
                schema_linking_path=resolved_schema_linking_path,
                output_path=resolved_output_path,
            )
        ]

    root_dir = resolve_path(metadata_dir or DEFAULT_METADATA_DIR)
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata directory does not exist: {root_dir}")
    if root_dir.is_file():
        raise ValueError(f"Expected metadata directory, got file: {root_dir}")

    direct_schema_linking_path = root_dir / schema_linking_filename
    if direct_schema_linking_path.exists():
        return root_dir, [
            CaseInput(
                case_dir=root_dir,
                relative_case_dir=Path(root_dir.name),
                schema_linking_path=direct_schema_linking_path.resolve(),
                output_path=(root_dir / output_filename).resolve(),
            )
        ]

    schema_linking_paths = sorted(
        {path.resolve() for path in root_dir.rglob(schema_linking_filename)}
    )
    if not schema_linking_paths:
        raise FileNotFoundError(
            f"No `{schema_linking_filename}` files were found under {root_dir}."
        )

    cases: list[CaseInput] = []
    for path in schema_linking_paths:
        case_dir = path.parent
        cases.append(
            CaseInput(
                case_dir=case_dir,
                relative_case_dir=case_dir.relative_to(root_dir),
                schema_linking_path=path,
                output_path=(case_dir / output_filename).resolve(),
            )
        )
    return root_dir, cases


def extract_context(
    *,
    case_dir: Path,
    schema_linking_payload: dict[str, Any],
    args: argparse.Namespace,
) -> dict[str, str]:
    context = read_sidecar_context(case_dir)
    context["sub_questions"] = read_sub_questions(
        case_dir=case_dir,
        nl2er_output_path=getattr(args, "nl2er_output_path", None),
        nl2er_output_filename=str(
            getattr(args, "nl2er_output_filename", DEFAULT_NL2ER_OUTPUT_FILENAME)
            or DEFAULT_NL2ER_OUTPUT_FILENAME
        ),
    )
    question_payload = schema_linking_payload.get("question")
    if isinstance(question_payload, dict):
        context["question_id"] = context["question_id"] or str(
            question_payload.get("question_id") or ""
        ).strip()
        context["user_intent"] = context["user_intent"] or str(
            question_payload.get("question") or ""
        ).strip()
        context["db_id"] = context["db_id"] or str(question_payload.get("db_id") or "").strip()

    normalized = normalize_schema_linking_payload(schema_linking_payload)
    if not context["db_id"]:
        for payload in normalized.values():
            db_id = str(payload.get("db_id") or "").strip() if isinstance(payload, dict) else ""
            if db_id:
                context["db_id"] = db_id
                break
    if not context["user_intent"]:
        for payload in normalized.values():
            question = (
                str(payload.get("question") or "").strip()
                if isinstance(payload, dict)
                else ""
            )
            if question:
                context["user_intent"] = question
                break

    if args.question is not None:
        context["user_intent"] = str(args.question).strip()
    if args.schema_linking_path is not None and args.question_id:
        context["question_id"] = sorted(normalize_name_filters(args.question_id))[0]
    if args.db_id is not None:
        context["db_id"] = str(args.db_id).strip()
    if args.db_hint:
        context["db_hint"] = str(args.db_hint).strip()
    if args.external_knowledge:
        context["external_knowledge"] = str(args.external_knowledge).strip()
    if not context["question_id"]:
        context["question_id"] = case_dir.name
    return context


def collect_linked_tables_and_columns(
    schema_linking_payload: dict[str, Any],
) -> tuple[list[str], list[str]]:
    normalized = normalize_schema_linking_payload(schema_linking_payload)
    table_set: set[str] = set()
    column_set: set[str] = set()
    for payload in normalized.values():
        if not isinstance(payload, dict):
            continue
        for table in payload.get("linked_tables") or []:
            text = str(table or "").strip()
            if text:
                table_set.add(text)
        for column in payload.get("linked_columns") or []:
            text = str(column or "").strip()
            if not text:
                continue
            column_set.add(text)
            table_fullname = table_fullname_from_column(text)
            if table_fullname:
                table_set.add(table_fullname)
    return sorted(table_set), sorted(column_set)


def get_embedded_schema_snapshot(schema_linking_payload: dict[str, Any]) -> dict[str, Any]:
    question_payload = schema_linking_payload.get("question")
    if not isinstance(question_payload, dict):
        return {}
    snapshot = question_payload.get("schema_snapshot")
    return snapshot if isinstance(snapshot, dict) else {}


def build_table_lookup(schema_evidence: dict[str, Any]) -> dict[str, dict[str, Any]]:
    lookup: dict[str, dict[str, Any]] = {}
    for table in ensure_dict_list(schema_evidence.get("tables")):
        fullname = str(table.get("table_fullname") or "").strip()
        if fullname:
            lookup[fullname.casefold()] = table
    return lookup


def columns_for_table(table_fullname: str, linked_columns: list[str]) -> list[str]:
    target = table_fullname.casefold()
    output: list[str] = []
    for column in linked_columns:
        if table_fullname_from_column(column).casefold() == target:
            output.append(column)
    return output


def shorten_text(value: Any, max_chars: int) -> str:
    text = str(value)
    if max_chars < 1 or len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def is_binary_sample_text(value: str) -> bool:
    text = str(value or "")
    return "bytearray(b" in text


def sanitize_sample_value(
    value: Any,
    *,
    max_chars: int,
) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return BINARY_SAMPLE_PLACEHOLDER
    if isinstance(value, (dict, list)):
        value = json.dumps(value, ensure_ascii=False)

    text = str(value)
    if is_binary_sample_text(text):
        return BINARY_SAMPLE_PLACEHOLDER
    return shorten_text(text, max_chars)


def sanitize_column_samples(
    columns: Any,
    *,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> list[dict[str, Any]]:
    sanitized_columns: list[dict[str, Any]] = []
    for column in ensure_dict_list(columns):
        payload = dict(column)
        sample_values = payload.get("sample_values")
        if isinstance(sample_values, list):
            payload["sample_values"] = [
                sanitize_sample_value(value, max_chars=sample_value_max_chars)
                for value in sample_values[:sample_values_per_column]
            ]
        sanitized_columns.append(payload)
    return sanitized_columns


def sanitize_sample_rows(
    sample_rows: Any,
    *,
    row_limit: int,
    sample_value_max_chars: int,
) -> list[dict[str, Any]]:
    if not isinstance(sample_rows, list) or row_limit == 0:
        return []
    sanitized_rows: list[dict[str, Any]] = []
    for row in sample_rows[: max(0, row_limit)]:
        if not isinstance(row, dict):
            continue
        sanitized_rows.append(
            {
                str(key): sanitize_sample_value(value, max_chars=sample_value_max_chars)
                for key, value in row.items()
            }
        )
    return sanitized_rows


def sanitize_table_snapshot(
    table: dict[str, Any],
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> dict[str, Any]:
    sanitized = dict(table)
    for key in ("columns", "available_columns", "linked_columns"):
        if key in sanitized:
            sanitized[key] = sanitize_column_samples(
                sanitized.get(key),
                sample_values_per_column=sample_values_per_column,
                sample_value_max_chars=sample_value_max_chars,
            )
    for key in ("sample_rows", "linked_sample_rows"):
        if key in sanitized:
            sanitized[key] = sanitize_sample_rows(
                sanitized.get(key),
                row_limit=sample_row_limit,
                sample_value_max_chars=sample_value_max_chars,
            )
    return sanitized


def build_target_table_prompt_snapshot(table: dict[str, Any]) -> dict[str, Any]:
    snapshot = dict(table)
    raw_columns = snapshot.get("columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raw_columns = snapshot.get("available_columns")
    if not isinstance(raw_columns, list) or not raw_columns:
        raw_columns = snapshot.get("linked_columns")
    if not isinstance(raw_columns, list):
        raw_columns = []

    columns: list[dict[str, Any]] = []
    for column in ensure_dict_list(raw_columns):
        payload = {
            key: value
            for key, value in column.items()
            if key != "is_linked_column"
        }
        columns.append(payload)

    if "sample_rows" not in snapshot and isinstance(snapshot.get("linked_sample_rows"), list):
        snapshot["sample_rows"] = snapshot.get("linked_sample_rows")

    snapshot["columns"] = columns
    snapshot.pop("available_columns", None)
    snapshot.pop("linked_columns", None)
    snapshot.pop("linked_sample_rows", None)
    return snapshot


def unique_nonempty_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        candidates = [values]
    elif isinstance(values, list):
        candidates = values
    else:
        return []

    output: list[str] = []
    seen: set[str] = set()
    for value in candidates:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def collect_evidence_columns_from_unit(unit: dict[str, Any]) -> list[str]:
    evidence_columns: list[str] = []
    evidence_columns.extend(unique_nonempty_strings(unit.get("evidence_columns")))
    for attribute in ensure_dict_list(unit.get("attributes")):
        evidence_columns.extend(unique_nonempty_strings(attribute.get("evidence_columns")))
    for participant in ensure_dict_list(unit.get("participants")):
        evidence_columns.extend(unique_nonempty_strings(participant.get("evidence_columns")))
    return unique_nonempty_strings(evidence_columns)


def collect_related_attributes_from_unit(unit: dict[str, Any]) -> list[str]:
    related_attributes = unique_nonempty_strings(unit.get("related_attributes"))
    for attribute in ensure_dict_list(unit.get("attributes")):
        name = str(attribute.get("name") or "").strip()
        if name:
            related_attributes.append(name)
    return unique_nonempty_strings(related_attributes)


def normalize_model_attribute(attribute: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": str(attribute.get("name") or attribute.get("attribute_name") or "").strip(),
        "semantics": str(
            attribute.get("semantics")
            or attribute.get("semantic")
            or attribute.get("desc")
            or attribute.get("description")
            or ""
        ).strip(),
        "evidence_columns": unique_nonempty_strings(attribute.get("evidence_columns")),
    }


def normalize_model_participant(participant: dict[str, Any]) -> dict[str, Any]:
    return {
        "role": str(participant.get("role") or "").strip(),
        "entity": str(participant.get("entity") or participant.get("entity_name") or "").strip(),
        "anchor_attribute": unique_nonempty_strings(
            participant.get("anchor_attribute")
            or participant.get("anchor_attributes")
            or participant.get("identifier_attrs")
            or participant.get("primary_key")
        ),
        "evidence_columns": unique_nonempty_strings(participant.get("evidence_columns")),
    }


def normalize_model_profile(
    *,
    parsed: dict[str, Any],
    table_fullname: str,
    target_table: dict[str, Any],
) -> dict[str, Any]:
    output_table_fullname = str(parsed.get("table_fullname") or table_fullname).strip()
    supported_semantics = unique_nonempty_strings(
        parsed.get("supported_question_semantics")
    )
    semantics = str(parsed.get("semantics") or "").strip()
    if semantics and not supported_semantics:
        supported_semantics = [semantics]

    raw_units = parsed.get("semantic_units")
    if not isinstance(raw_units, list):
        raw_units = parsed.get("units")
    if not isinstance(raw_units, list):
        raw_units = []

    semantic_units: list[dict[str, Any]] = []
    for raw_unit in raw_units:
        if not isinstance(raw_unit, dict):
            continue
        unit_type = str(raw_unit.get("unit_type") or "").strip()
        unit_name = str(raw_unit.get("unit_name") or raw_unit.get("name") or "").strip()
        attributes = [
            normalize_model_attribute(attribute)
            for attribute in ensure_dict_list(raw_unit.get("attributes"))
        ]
        if not attributes:
            legacy_evidence_columns = unique_nonempty_strings(raw_unit.get("evidence_columns"))
            legacy_related_attributes = collect_related_attributes_from_unit(raw_unit)
            if legacy_related_attributes:
                attributes = [
                    {
                        "name": attribute_name,
                        "semantics": "",
                        "evidence_columns": legacy_evidence_columns,
                    }
                    for attribute_name in legacy_related_attributes
                ]
            elif legacy_evidence_columns:
                attributes = [
                    {
                        "name": "table_support",
                        "semantics": "",
                        "evidence_columns": legacy_evidence_columns,
                    }
                ]
        participants = [
            normalize_model_participant(participant)
            for participant in ensure_dict_list(raw_unit.get("participants"))
        ]
        semantic_units.append(
            {
                "unit_type": unit_type,
                "unit_name": unit_name,
                "desc": str(raw_unit.get("desc") or "").strip(),
                "grain": str(raw_unit.get("grain") or "").strip(),
                "attributes": attributes,
                "participants": participants if unit_type == "relationship" else [],
            }
        )

    is_relevant = parsed.get("is_relevant")
    if not isinstance(is_relevant, bool):
        is_relevant = bool(supported_semantics or semantic_units)

    return {
        "table_fullname": output_table_fullname,
        "supported_question_semantics": supported_semantics,
        "is_relevant": is_relevant,
        "semantic_units": semantic_units,
    }


def iter_profile_units(sketch: dict[str, Any]) -> list[dict[str, Any]]:
    units = sketch.get("semantic_units")
    if isinstance(units, list):
        return [unit for unit in units if isinstance(unit, dict)]
    units = sketch.get("units")
    if isinstance(units, list):
        return [unit for unit in units if isinstance(unit, dict)]
    return []


def collect_output_schema_selection(
    *,
    table_payloads: list[dict[str, Any]],
    sketches: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    table_names: list[str] = []
    seen_tables: set[str] = set()
    column_names: list[str] = []
    seen_columns: set[str] = set()

    def add_table(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_tables:
            return
        table_names.append(text)
        seen_tables.add(text)

    def resolve_column_fullname(value: Any, item: dict[str, Any] | None = None) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if table_fullname_from_column(text):
            return text
        if item is None:
            return ""

        target_table = item.get("target_table")
        if not isinstance(target_table, dict):
            return ""
        normalized_text = text.casefold()
        for key in ("linked_columns", "columns", "available_columns"):
            for column in ensure_dict_list(target_table.get(key)):
                column_name = str(column.get("column_name") or "").strip()
                column_fullname = str(column.get("column_fullname") or "").strip()
                if column_fullname and column_name.casefold() == normalized_text:
                    return column_fullname
        return ""

    def add_column(value: Any, item: dict[str, Any] | None = None) -> None:
        text = resolve_column_fullname(value, item)
        if not text or text in seen_columns:
            return
        column_names.append(text)
        seen_columns.add(text)
        table_fullname = table_fullname_from_column(text)
        if table_fullname:
            add_table(table_fullname)

    for item in table_payloads:
        add_table(item.get("table_fullname"))
        for column in unique_nonempty_strings(item.get("linked_columns")):
            add_column(column, item)

    for item, sketch in zip(table_payloads, sketches):
        for unit in iter_profile_units(sketch):
            for column in collect_evidence_columns_from_unit(unit):
                add_column(column, item)

    return table_names, column_names


def resolve_table_sketch_concurrency(requested_concurrency: int | None) -> int:
    requested = requested_concurrency or DEFAULT_TABLE_SKETCH_CONCURRENCY
    return max(1, min(requested, MAX_TABLE_SKETCH_CONCURRENCY))


def build_schema_linking_with_overall_result(
    *,
    schema_linking_payload: dict[str, Any],
    context: dict[str, str],
    linked_tables: list[str],
    linked_columns: list[str],
    source: str,
) -> dict[str, Any]:
    output_payload = dict(schema_linking_payload)
    existing_question = output_payload.get("question")
    question_payload = dict(existing_question) if isinstance(existing_question, dict) else {}
    existing_schema_linking = question_payload.get("schema_linking")
    schema_linking_meta = (
        dict(existing_schema_linking) if isinstance(existing_schema_linking, dict) else {}
    )
    schema_linking_meta.update(
        {
            "ok": bool(linked_tables or linked_columns),
            "source": source,
            "table_count": len(linked_tables),
            "column_count": len(linked_columns),
        }
    )

    question_payload.update(
        {
            "unit_name": str(question_payload.get("unit_name") or "question"),
            "unit_type": str(question_payload.get("unit_type") or "question"),
            "question_id": context.get("question_id", ""),
            "db_id": context.get("db_id", ""),
            "question": context.get("user_intent", ""),
            "sub_questions": context.get("sub_questions", ""),
            "linked_tables": linked_tables,
            "linked_columns": linked_columns,
            "schema_linking": schema_linking_meta,
        }
    )
    output_payload["question"] = question_payload
    return output_payload


class TableSemanticSketchRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        database_root: str | Path | None = None,
        spider2_root: str | Path | None = None,
        sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
        dry_run: bool = False,
        initialize_llm: bool = True,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = resolve_path(log_dir)
        self.model_config = model_config
        self.database_root = database_root
        self.spider2_root = spider2_root
        self.sample_values_per_column = sample_values_per_column
        self.dry_run = dry_run
        self.prompt_output_dir = self.log_dir / "table_prompts"
        self.response_output_dir = self.log_dir / "table_responses"
        for directory in (self.log_dir, self.prompt_output_dir, self.response_output_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.llm: LLMClient | None = None
        if not self.dry_run and initialize_llm:
            settings = load_settings()
            config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
            self.llm = LLMClient(config)

        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self.prompt_builder.register_template(
            name=TEMPLATE_KEY,
            template_name=TEMPLATE_NAME,
            required_vars=[
                "user_intent",
                "sub_questions",
                "db_id",
                "db_hint",
                "external_knowledge",
                "target_table",
            ],
        )

    def build_prompt(
        self,
        *,
        context: dict[str, str],
        target_table: dict[str, Any],
    ) -> str:
        prompt_target_table = build_target_table_prompt_snapshot(target_table)
        sub_questions = str(context.get("sub_questions") or "").strip()
        if sub_questions:
            prompt_target_table["question_context"] = {
                "sub_questions": sub_questions,
            }
        return self.prompt_builder.build_text(
            TEMPLATE_KEY,
            vars={
                "user_intent": context.get("user_intent", ""),
                "sub_questions": context.get("sub_questions", ""),
                "db_id": context.get("db_id", ""),
                "db_hint": context.get("db_hint", ""),
                "external_knowledge": context.get("external_knowledge", ""),
                "target_table": prompt_target_table,
            },
        )

    @staticmethod
    def build_dry_run_sketch(
        *,
        table_fullname: str,
        target_table: dict[str, Any],
        linked_columns: list[str],
    ) -> dict[str, Any]:
        related_attributes = []
        evidence_columns = []
        for column in ensure_dict_list(target_table.get("columns")):
            column_fullname = str(column.get("column_fullname") or "").strip()
            column_name = str(column.get("column_name") or column_fullname).strip()
            if not column_name:
                continue
            related_attributes.append(column_name)
            evidence_columns.append(column_fullname or column_name)
        if linked_columns:
            evidence_columns = linked_columns
        return {
            "table_fullname": table_fullname,
            "supported_question_semantics": [
                "Dry-run placeholder: inspect the generated prompt for the intended logical support."
            ] if related_attributes or linked_columns else [],
            "is_relevant": bool(related_attributes or linked_columns),
            "semantic_units": [
                {
                    "unit_type": "entity",
                    "unit_name": str(target_table.get("table_name") or table_fullname),
                    "desc": "Dry-run placeholder semantic unit.",
                    "grain": "Dry-run placeholder grain.",
                    "attributes": [
                        {
                            "name": attr_name,
                            "semantics": "Dry-run placeholder attribute support.",
                            "evidence_columns": [
                                evidence_columns[index]
                            ] if index < len(evidence_columns) else [],
                        }
                        for index, attr_name in enumerate(related_attributes)
                    ],
                    "participants": [],
                }
            ] if related_attributes or linked_columns else [],
        }

    def analyze_tables(
        self,
        *,
        context: dict[str, str],
        table_payloads: list[dict[str, Any]],
        max_concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        items, prompts = self.prepare_table_prompts(
            context=context,
            table_payloads=table_payloads,
        )

        if self.dry_run:
            return [
                self.build_dry_run_sketch(
                    table_fullname=item["table_fullname"],
                    target_table=item["target_table"],
                    linked_columns=item["linked_columns"],
                )
                for item in items
            ]

        if self.llm is None:
            raise RuntimeError("LLM client is not initialized. Disable --dry-run to call the LLM.")

        raw_responses = self.llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(prompts) > 1,
            progress_desc="Table semantic sketch",
        )
        return self.parse_table_responses(items=items, raw_responses=raw_responses)

    def prepare_table_prompts(
        self,
        *,
        context: dict[str, str],
        table_payloads: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        prompts: list[str] = []
        items: list[dict[str, Any]] = []
        for item in table_payloads:
            table_fullname = item["table_fullname"]
            prompt = self.build_prompt(
                context=context,
                target_table=item["target_table"],
            )
            stem = safe_file_stem(table_fullname)
            prompt_path = self.prompt_output_dir / f"{stem}.md"
            response_path = self.response_output_dir / f"{stem}.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            item = {
                **item,
                "prompt_path": str(prompt_path),
                "response_path": str(response_path),
            }
            items.append(item)
            prompts.append(prompt)
        return items, prompts

    def parse_table_responses(
        self,
        *,
        items: list[dict[str, Any]],
        raw_responses: list[str],
    ) -> list[dict[str, Any]]:
        sketches: list[dict[str, Any]] = []
        for item, raw_response in zip(items, raw_responses):
            response_path = Path(item["response_path"])
            response_path.write_text(raw_response or "", encoding="utf-8")
            table_fullname = item["table_fullname"]
            if not raw_response or not raw_response.strip():
                sketches.append(
                    self.build_error_sketch(
                        table_fullname=table_fullname,
                        error="LLM returned empty response.",
                        item=item,
                    )
                )
                continue
            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                sketches.append(
                    self.build_error_sketch(
                        table_fullname=table_fullname,
                        error=f"Failed to parse table semantic sketch JSON: {exc}",
                        item=item,
                    )
                )
                continue
            profile = normalize_model_profile(
                parsed=parsed,
                table_fullname=table_fullname,
                target_table=item["target_table"],
            )
            if "semantic_units" not in parsed and "units" not in parsed:
                profile["warning"] = "`semantic_units` was missing or not a list."
            sketches.append(profile)
        return sketches

    @staticmethod
    def build_error_sketch(
        *,
        table_fullname: str,
        error: str,
        item: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "table_fullname": table_fullname,
            "supported_question_semantics": [],
            "is_relevant": False,
            "semantic_units": [],
            "error": error,
        }

    def prepare_case(
        self,
        *,
        case_input: CaseInput,
        args: argparse.Namespace,
    ) -> PreparedCase:
        started_at = time.perf_counter()
        schema_linking_payload = read_json_object(case_input.schema_linking_path)
        context = extract_context(
            case_dir=case_input.case_dir,
            schema_linking_payload=schema_linking_payload,
            args=args,
        )
        if not context.get("user_intent"):
            raise ValueError(
                "Question text is required. Provide --question or an input.json/schema_linking question."
            )
        if not context.get("db_id"):
            raise ValueError("db_id is required. Provide --db-id or include it in inputs.")

        linked_tables, linked_columns = collect_linked_tables_and_columns(schema_linking_payload)
        table_filters = normalize_table_filters(args.table_fullname)
        if table_filters:
            linked_tables = [
                table for table in linked_tables if table.casefold() in table_filters
            ]
        if not linked_tables:
            raise ValueError("No linked tables were found to analyze.")

        schema_evidence = get_embedded_schema_snapshot(schema_linking_payload)
        if not schema_evidence:
            schema_linking_by_unit = normalize_schema_linking_payload(schema_linking_payload)
            schema_evidence = build_schema_evidence(
                db_id=context["db_id"],
                schema_linking_by_unit=schema_linking_by_unit,
                database_root=self.database_root,
                spider2_root=self.spider2_root,
                sample_values_per_column=self.sample_values_per_column,
            )
        table_lookup = build_table_lookup(schema_evidence)

        table_payloads = []
        for table_fullname in linked_tables:
            target_table = table_lookup.get(table_fullname.casefold()) or {
                "table_fullname": table_fullname,
                "table_name": table_fullname,
                "columns": [],
                "sample_rows": [],
                "warning": "No schema snapshot was found for this linked table.",
            }
            target_table = sanitize_table_snapshot(
                target_table,
                sample_row_limit=args.sample_row_limit,
                sample_values_per_column=self.sample_values_per_column,
                sample_value_max_chars=args.sample_value_max_chars,
            )
            table_payloads.append(
                {
                    "table_fullname": table_fullname,
                    "target_table": target_table,
                    "linked_columns": columns_for_table(table_fullname, linked_columns),
                }
        )

        write_json(self.log_dir / "run_context.json", context)
        write_json(self.log_dir / "schema_linking.json", schema_linking_payload)
        write_json(self.log_dir / "schema_evidence.json", schema_evidence)

        return PreparedCase(
            case_input=case_input,
            runner=self,
            context=context,
            schema_linking_payload=schema_linking_payload,
            table_payloads=table_payloads,
            started_at=started_at,
        )

    def finalize_case(
        self,
        *,
        prepared: PreparedCase,
        sketches: list[dict[str, Any]],
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        case_input = prepared.case_input
        context = prepared.context
        schema_linking_payload = prepared.schema_linking_payload
        table_payloads = prepared.table_payloads
        ok = all(not item.get("error") for item in sketches)
        output_linked_tables, output_linked_columns = collect_output_schema_selection(
            table_payloads=table_payloads,
            sketches=sketches,
        )
        schema_linking_with_overall = build_schema_linking_with_overall_result(
            schema_linking_payload=schema_linking_payload,
            context=context,
            linked_tables=output_linked_tables,
            linked_columns=output_linked_columns,
            source="table_semantic_sketch_output",
        )
        schema_linking_with_overall["linked_table_profiles"] = sketches
        output_payload = {
            "ok": ok,
            "question_id": context.get("question_id", ""),
            "db_id": context.get("db_id", ""),
            "question": context.get("user_intent", ""),
            "sub_questions": context.get("sub_questions", ""),
            "schema_linking_path": str(case_input.schema_linking_path),
            "output_path": str(case_input.output_path),
            "log_dir": str(self.log_dir),
            "table_count": len(sketches),
            "linked_table_profiles": sketches,
            "elapsed_seconds": time.perf_counter() - prepared.started_at,
        }
        write_json(self.log_dir / "schema_linking.json", schema_linking_with_overall)
        if bool(getattr(args, "write_enriched_schema_linking", False)):
            write_json(case_input.schema_linking_path, schema_linking_with_overall)
        write_json(case_input.output_path, output_payload)
        write_json(self.log_dir / "output.json", output_payload)
        return output_payload

    def run_case(
        self,
        *,
        case_input: CaseInput,
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        prepared = self.prepare_case(case_input=case_input, args=args)
        sketch_started_at = time.perf_counter()
        requested_table_concurrency = (
            getattr(args, "max_table_concurrency", None)
            or getattr(args, "max_workers", None)
        )
        sketches = self.analyze_tables(
            context=prepared.context,
            table_payloads=prepared.table_payloads,
            max_concurrency=resolve_table_sketch_concurrency(requested_table_concurrency),
        )
        emit_step_done_log(
            prefix="TABLE_SEMANTIC_SKETCH",
            step="table_analysis",
            elapsed_seconds=time.perf_counter() - sketch_started_at,
            tables=len(sketches),
            dry_run=bool(self.dry_run),
        )

        return self.finalize_case(prepared=prepared, sketches=sketches, args=args)


def run_single_case(
    *,
    case_input: CaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir

    runner = TableSemanticSketchRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        database_root=args.database_root,
        spider2_root=args.spider2_root,
        sample_values_per_column=args.sample_values_per_column,
        dry_run=bool(args.dry_run),
    )
    payload = runner.run_case(case_input=case_input, args=args)
    summary = {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id", ""),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "schema_linking_path": str(case_input.schema_linking_path),
        "output_path": str(case_input.output_path),
        "log_dir": str(log_dir),
        "table_count": int(payload.get("table_count") or 0),
        "dry_run": bool(args.dry_run),
    }
    write_json(log_dir / "case_summary.json", summary)
    return summary


def run_single_case_safe(
    *,
    case_input: CaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    try:
        return run_single_case(
            case_input=case_input,
            run_timestamp=run_timestamp,
            args=args,
        )
    except Exception as exc:
        log_dir.mkdir(parents=True, exist_ok=True)
        failure_payload = {
            "ok": False,
            "question_id": case_input.case_dir.name,
            "case_dir": str(case_input.case_dir),
            "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
            "schema_linking_path": str(case_input.schema_linking_path),
            "output_path": str(case_input.output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "dry_run": bool(args.dry_run),
        }
        write_json(log_dir / "error.json", failure_payload)
        write_json(case_input.output_path, failure_payload)
        return failure_payload


def prepare_single_case_for_batch_safe(
    *,
    case_input: CaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> PreparedCase | dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir

    try:
        runner = TableSemanticSketchRunner(
            prompt_dir=args.prompt_dir,
            log_dir=log_dir,
            model_config=args.model_config,
            reasoning_mode=args.reasoning_mode,
            database_root=args.database_root,
            spider2_root=args.spider2_root,
            sample_values_per_column=args.sample_values_per_column,
            dry_run=bool(args.dry_run),
            initialize_llm=False,
        )
        return runner.prepare_case(case_input=case_input, args=args)
    except Exception as exc:
        log_dir.mkdir(parents=True, exist_ok=True)
        failure_payload = {
            "ok": False,
            "question_id": case_input.case_dir.name,
            "case_dir": str(case_input.case_dir),
            "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
            "schema_linking_path": str(case_input.schema_linking_path),
            "output_path": str(case_input.output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "dry_run": bool(args.dry_run),
        }
        write_json(log_dir / "error.json", failure_payload)
        write_json(case_input.output_path, failure_payload)
        return failure_payload


def summarize_case_payload(
    *,
    payload: dict[str, Any],
    case_input: CaseInput,
    log_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    summary = {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id", ""),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "schema_linking_path": str(case_input.schema_linking_path),
        "output_path": str(case_input.output_path),
        "log_dir": str(log_dir),
        "table_count": int(payload.get("table_count") or 0),
        "dry_run": bool(args.dry_run),
    }
    write_json(log_dir / "case_summary.json", summary)
    return summary


def finalize_prepared_case(
    *,
    prepared: PreparedCase,
    sketches: list[dict[str, Any]],
    args: argparse.Namespace,
    sketch_elapsed_seconds: float,
) -> dict[str, Any]:
    emit_step_done_log(
        prefix="TABLE_SEMANTIC_SKETCH",
        step="table_analysis",
        elapsed_seconds=sketch_elapsed_seconds,
        tables=len(sketches),
        dry_run=bool(prepared.runner.dry_run),
    )
    payload = prepared.runner.finalize_case(
        prepared=prepared,
        sketches=sketches,
        args=args,
    )
    return summarize_case_payload(
        payload=payload,
        case_input=prepared.case_input,
        log_dir=prepared.runner.log_dir,
        args=args,
    )


def main() -> None:
    args = parse_args()
    batch_root, case_inputs = discover_case_inputs(
        metadata_dir=args.metadata_dir,
        schema_linking_path=args.schema_linking_path,
        output_path=args.output_path,
        schema_linking_filename=args.schema_linking_filename,
        output_filename=args.output_filename,
    )
    if args.schema_linking_path is None:
        case_inputs = filter_cases_by_question_id(
            cases=case_inputs,
            requested_question_ids=normalize_name_filters(args.question_id),
        )
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()

    print(f"[TABLE_SEMANTIC_SKETCH] batch_root={batch_root}")
    print(f"[TABLE_SEMANTIC_SKETCH] discovered_cases={len(case_inputs)}")
    print(f"[TABLE_SEMANTIC_SKETCH] run_timestamp={run_timestamp}")
    print(f"[TABLE_SEMANTIC_SKETCH] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[TABLE_SEMANTIC_SKETCH] log_root={resolve_path(args.log_root)}")
    print(f"[TABLE_SEMANTIC_SKETCH] dry_run={bool(args.dry_run)}")

    completed_summaries: list[dict[str, Any]] = []
    prepared_cases: list[PreparedCase] = []
    for case_input in case_inputs:
        prepared_or_failure = prepare_single_case_for_batch_safe(
            case_input=case_input,
            run_timestamp=run_timestamp,
            args=args,
        )
        if isinstance(prepared_or_failure, PreparedCase):
            prepared_cases.append(prepared_or_failure)
        else:
            completed_summaries.append(prepared_or_failure)

    global_prompts: list[str] = []
    case_items: list[list[dict[str, Any]]] = []
    case_raw_responses: list[list[str]] = [[] for _ in prepared_cases]
    item_case_indexes: list[int] = []

    for case_index, prepared in enumerate(prepared_cases):
        items, prompts = prepared.runner.prepare_table_prompts(
            context=prepared.context,
            table_payloads=prepared.table_payloads,
        )
        case_items.append(items)
        for item, prompt in zip(items, prompts):
            global_prompts.append(prompt)
            item_case_indexes.append(case_index)

    print(
        "[TABLE_SEMANTIC_SKETCH] prepared "
        f"cases={len(prepared_cases)} "
        f"failed_cases={len(completed_summaries)} "
        f"table_prompt_count={len(global_prompts)}"
    )

    sketch_started_at = time.perf_counter()
    if args.dry_run:
        for case_index, prepared in enumerate(prepared_cases):
            case_raw_responses[case_index] = []
    elif global_prompts:
        requested_table_concurrency = args.max_table_concurrency or args.max_workers
        max_table_concurrency = resolve_table_sketch_concurrency(requested_table_concurrency)
        print(
            "[TABLE_SEMANTIC_SKETCH] global_table_sketch_batch "
            f"case_count={len(prepared_cases)} "
            f"table_prompt_count={len(global_prompts)} "
            f"max_concurrency={max_table_concurrency}"
        )
        settings = load_settings()
        config = apply_reasoning_mode(settings.llm.get(args.model_config), args.reasoning_mode)
        llm = LLMClient(config)
        raw_responses = llm.batch_single_turn(
            global_prompts,
            check_func=json_check,
            max_concurrency=max_table_concurrency,
            show_progress=len(global_prompts) > 1,
            progress_desc="Table semantic sketch",
        )
        for case_index, raw_response in zip(item_case_indexes, raw_responses):
            case_raw_responses[case_index].append(raw_response)

    sketch_elapsed_seconds = time.perf_counter() - sketch_started_at
    for case_index, prepared in enumerate(prepared_cases):
        if args.dry_run:
            sketches = [
                prepared.runner.build_dry_run_sketch(
                    table_fullname=item["table_fullname"],
                    target_table=item["target_table"],
                    linked_columns=item["linked_columns"],
                )
                for item in case_items[case_index]
            ]
        else:
            sketches = prepared.runner.parse_table_responses(
                items=case_items[case_index],
                raw_responses=case_raw_responses[case_index],
            )
        summary = finalize_prepared_case(
            prepared=prepared,
            sketches=sketches,
            args=args,
            sketch_elapsed_seconds=sketch_elapsed_seconds,
        )
        completed_summaries.append(summary)

    success_count = sum(1 for item in completed_summaries if item.get("ok"))
    print(
        "[TABLE_SEMANTIC_SKETCH] finalized "
        f"cases={len(completed_summaries)} "
        f"ok_cases={success_count} "
        f"failed_cases={len(completed_summaries) - success_count}"
    )
    batch_summary = {
        "timestamp": run_timestamp,
        "batch_root": str(batch_root),
        "log_root": str(resolve_path(args.log_root)),
        "case_count": len(case_inputs),
        "ok_cases": success_count,
        "failed_cases": len(case_inputs) - success_count,
        "dry_run": bool(args.dry_run),
        "cases": completed_summaries,
    }
    batch_summary_path = resolve_path(args.log_root) / run_timestamp / "batch_summary.json"
    write_json(batch_summary_path, batch_summary)
    emit_step_done_log(
        prefix="TABLE_SEMANTIC_SKETCH",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
