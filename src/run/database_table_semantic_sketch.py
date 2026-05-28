from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.er2data.physical_schema import (
    TableMetadata as ComponentTableMetadata,
    load_database_tables as component_load_database_tables,
    table_metadata_to_prompt_snapshot as component_table_metadata_to_prompt_snapshot,
)
from src.er2data.table_grouping import (
    GROUPING_METHOD as COMPONENT_GROUPING_METHOD,
    TableGroup as ComponentTableGroup,
    build_group_key as component_build_group_key,
    build_group_prompt_snapshot as component_build_group_prompt_snapshot,
    build_table_groups as component_build_table_groups,
    clear_tb as component_clear_tb,
    column_signature_hash as component_column_signature_hash,
    filter_groups_by_table_fullname as component_filter_groups_by_table_fullname,
    group_payloads_from_groups as component_group_payloads_from_groups,
    normalize_casefold_set as component_normalize_casefold_set,
    normalize_selected_member_tables as component_normalize_selected_member_tables,
    normalized_column_signature as component_normalized_column_signature,
    remove_digits as component_remove_digits,
    resolve_group_column_fullnames as component_resolve_group_column_fullnames,
    serialize_group as component_serialize_group,
    serialize_table_member as component_serialize_table_member,
)
from src.nl2er.table_semantic_sketch import (
    build_compact_semantic_output as component_build_compact_semantic_output,
    collect_column_references as component_collect_column_references,
    collect_group_schema_selection as component_collect_group_schema_selection,
    collect_response_strings as component_collect_response_strings,
    extract_group_response_payload as component_extract_group_response_payload,
    first_nonempty_string as component_first_nonempty_string,
    nested_dict as component_nested_dict,
    normalize_bool as component_normalize_bool,
    normalize_database_group_profile as component_normalize_database_group_profile,
    profile_is_relevant_supportive as component_profile_is_relevant_supportive,
)
from src.er2data.schema_utils import (
    DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    column_name_from_fullname,
    ensure_dict_list,
    read_json_object,
    read_sidecar_context,
    resolve_path,
    resolve_snapshot_search_roots,
    safe_file_stem,
    sample_column_values,
    table_fullname_from_column,
    table_name_from_fullname,
)
from src.nl2er.input_payloads import positive_int, read_inputs, select_inputs
from src.nl2er.schema_linking_semantic import (
    DEFAULT_NL2ER_OUTPUT_FILENAME,
    DEFAULT_PROMPT_DIR,
    DEFAULT_SAMPLE_ROW_LIMIT,
    DEFAULT_SAMPLE_VALUE_MAX_CHARS,
    DEFAULT_TABLE_SKETCH_CONCURRENCY,
    MAX_TABLE_SKETCH_CONCURRENCY,
    build_schema_linking_with_overall_result,
    collect_evidence_columns_from_unit,
    normalize_model_profile,
    normalize_name_filters,
    read_sub_questions,
    resolve_table_sketch_concurrency,
    sanitize_table_snapshot,
    unique_nonempty_strings,
)
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


DEFAULT_METADATA_DIR = Path("metadata")
DEFAULT_LOG_ROOT = Path("log/database_table_semantic_sketch")
DEFAULT_OUTPUT_FILENAME = "database_table_semantic_sketch.json"
DEFAULT_SCHEMA_LINKING_OUTPUT_FILENAME = "schema_linking.json"
GROUPING_METHOD = COMPONENT_GROUPING_METHOD
OUTPUT_SOURCE = "database_table_semantic_sketch"
TEMPLATE_KEY = "database_table_semantic_sketch"
TEMPLATE_NAME = "Database_table_semantic_sketch_v0.1.md"


@dataclass(slots=True)
class DatabaseCaseInput:
    case_dir: Path
    relative_case_dir: Path
    output_path: Path | None
    context: dict[str, str] | None = None


@dataclass(frozen=True, slots=True)
class TableMetadata:
    full_name: str
    namespace: str
    short_name: str
    column_names: list[str]
    column_types: list[str]
    descriptions: list[str]
    sample_rows: list[dict[str, Any]]
    snapshot_path: str
    table_description: str = ""


@dataclass(frozen=True, slots=True)
class TableGroup:
    group_id: str
    representative: TableMetadata
    members: list[TableMetadata]
    group_key: dict[str, Any]


@dataclass(slots=True)
class PreparedDatabaseCase:
    case_input: DatabaseCaseInput
    runner: DatabaseTableSemanticSketchRunner
    context: dict[str, str]
    table_groups: list[TableGroup]
    group_payloads: list[dict[str, Any]]
    started_at: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Sketch question-oriented table semantics over the whole database by "
            "first grouping physical tables with the ReFoRCE table-family rule."
        )
    )
    parser.add_argument("--input-path", type=Path, default=None)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--nl2er-output-path", type=Path, default=None)
    parser.add_argument("--nl2er-output-filename", default=DEFAULT_NL2ER_OUTPUT_FILENAME)
    parser.add_argument("--question", default=None)
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run from --input-path or --metadata-dir. Supports comma-separated values.",
    )
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--db-hint", default="")
    parser.add_argument("--external-knowledge", default="")
    parser.add_argument(
        "--table-fullname",
        nargs="+",
        default=None,
        help="Optional table fullname filters. A group is kept when any member matches.",
    )
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
            "Maximum parallel group sketch LLM requests. Defaults to "
            f"{DEFAULT_TABLE_SKETCH_CONCURRENCY} and is capped at "
            f"{MAX_TABLE_SKETCH_CONCURRENCY}."
        ),
    )
    parser.add_argument(
        "--write-schema-linking",
        action="store_true",
        help="Write a schema-linking-compatible file next to the output.",
    )
    parser.add_argument(
        "--schema-linking-output-filename",
        default=DEFAULT_SCHEMA_LINKING_OUTPUT_FILENAME,
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def clear_tb(text: str) -> str:
    return str(text or "").replace('"', "").replace("`", "").strip().upper()


def remove_digits(text: str) -> str:
    return re.sub(r"\d", "", str(text or ""))


def normalize_casefold_set(values: list[str] | None) -> set[str]:
    filters: set[str] = set()
    for value in values or []:
        for item in str(value or "").split(","):
            text = item.strip()
            if text:
                filters.add(text.casefold())
    return filters


def normalize_context(
    *,
    case_input: DatabaseCaseInput,
    args: argparse.Namespace,
) -> dict[str, str]:
    context = dict(case_input.context or read_sidecar_context(case_input.case_dir))
    context["sub_questions"] = read_sub_questions(
        case_dir=case_input.case_dir,
        nl2er_output_path=getattr(args, "nl2er_output_path", None),
        nl2er_output_filename=str(
            getattr(args, "nl2er_output_filename", DEFAULT_NL2ER_OUTPUT_FILENAME)
            or DEFAULT_NL2ER_OUTPUT_FILENAME
        ),
    )
    if args.question is not None:
        context["user_intent"] = str(args.question).strip()
    if args.db_id is not None:
        context["db_id"] = str(args.db_id).strip()
    if args.db_hint:
        context["db_hint"] = str(args.db_hint).strip()
    if args.external_knowledge:
        context["external_knowledge"] = str(args.external_knowledge).strip()
    if args.question_id and not context["question_id"]:
        context["question_id"] = sorted(normalize_name_filters(args.question_id))[0]
    if not context["question_id"]:
        context["question_id"] = case_input.case_dir.name
    return context


def database_case_output_path(
    *,
    case_input: DatabaseCaseInput,
    log_dir: Path,
    output_filename: str,
) -> Path:
    return case_input.output_path or (log_dir / output_filename).resolve()


def build_context_from_input_payload(input_payload: Any) -> dict[str, str]:
    return {
        "question_id": str(input_payload.question_id),
        "user_intent": str(input_payload.user_intent),
        "db_id": str(input_payload.db_id),
        "db_hint": str(input_payload.db_hint),
        "external_knowledge": str(input_payload.external_knowledge),
    }


def discover_database_case_inputs(
    *,
    input_path: str | Path | None,
    metadata_dir: str | Path | None,
    output_path: str | Path | None,
    output_filename: str,
    requested_question_ids: set[str],
) -> tuple[Path, list[DatabaseCaseInput]]:
    if input_path is not None and metadata_dir is not None:
        raise ValueError("Use either --input-path or --metadata-dir, not both.")

    if input_path is not None:
        resolved_input_path = resolve_path(input_path)
        all_inputs = read_inputs(resolved_input_path)
        selected_inputs = select_inputs(all_inputs, sorted(requested_question_ids))
        if output_path is not None and len(selected_inputs) > 1:
            raise ValueError("--output-path can only be used with --input-path when one question is selected.")
        cases = [
            DatabaseCaseInput(
                case_dir=resolved_input_path.parent,
                relative_case_dir=Path(safe_file_stem(input_payload.question_id)),
                output_path=resolve_path(output_path) if output_path is not None else None,
                context=build_context_from_input_payload(input_payload),
            )
            for input_payload in selected_inputs
        ]
        return resolved_input_path, cases

    root_dir = resolve_path(metadata_dir or DEFAULT_METADATA_DIR)
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata directory does not exist: {root_dir}")
    if root_dir.is_file():
        raise ValueError(f"Expected metadata directory, got file: {root_dir}")

    direct_markers = [root_dir / "input.json", root_dir / "nl2er_input.json", root_dir / "run_context.json"]
    if any(path.exists() for path in direct_markers):
        resolved_output_path = (
            resolve_path(output_path)
            if output_path is not None
            else (root_dir / output_filename).resolve()
        )
        return root_dir, [
            DatabaseCaseInput(
                case_dir=root_dir,
                relative_case_dir=Path(root_dir.name),
                output_path=resolved_output_path,
            )
        ]

    marker_paths = sorted(
        {
            path.resolve()
            for marker_name in ("input.json", "nl2er_input.json", "run_context.json")
            for path in root_dir.rglob(marker_name)
        }
    )
    if not marker_paths:
        raise FileNotFoundError(
            f"No input.json, nl2er_input.json, or run_context.json files were found under {root_dir}."
        )

    case_dirs: list[Path] = []
    seen: set[str] = set()
    for marker_path in marker_paths:
        case_dir = marker_path.parent
        key = str(case_dir).casefold()
        if key in seen:
            continue
        case_dirs.append(case_dir)
        seen.add(key)

    cases = [
        DatabaseCaseInput(
            case_dir=case_dir,
            relative_case_dir=case_dir.relative_to(root_dir),
            output_path=(case_dir / output_filename).resolve(),
        )
        for case_dir in case_dirs
    ]
    return root_dir, cases


def filter_cases_by_question_id(
    *,
    cases: list[DatabaseCaseInput],
    requested_question_ids: set[str],
) -> list[DatabaseCaseInput]:
    if not requested_question_ids:
        return cases

    requested = {question_id.casefold() for question_id in requested_question_ids}
    selected: list[DatabaseCaseInput] = []
    seen: set[str] = set()
    for case in cases:
        context = case.context or read_sidecar_context(case.case_dir)
        question_id = str(context.get("question_id") or "").strip()
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
            "The following question_id values were not found: "
            + ", ".join(sorted(missing))
        )
    return selected


def normalized_column_signature(
    column_names: list[str],
    column_types: list[str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                (clear_tb(column_name), clear_tb(column_type))
                for column_name, column_type in zip(column_names, column_types)
            ),
            key=lambda item: (item[0], item[1]),
        )
    )


def column_signature_hash(column_names: list[str], column_types: list[str]) -> str:
    payload = json.dumps(
        normalized_column_signature(column_names, column_types),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def build_group_key(table: TableMetadata) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    columns_signature = normalized_column_signature(
        table.column_names,
        table.column_types,
    )
    return clear_tb(table.namespace), remove_digits(clear_tb(table.short_name)), columns_signature


def load_database_tables(
    *,
    db_id: str,
    database_root: str | Path | None,
    spider2_root: str | Path | None,
) -> list[TableMetadata]:
    search_roots = resolve_snapshot_search_roots(
        db_id=db_id,
        database_root=database_root,
        spider2_root=spider2_root,
    )
    if not search_roots:
        raise FileNotFoundError(
            f"No database schema roots were found for db_id `{db_id}`."
        )

    tables_by_fullname: dict[str, TableMetadata] = {}
    for root in search_roots:
        for json_path in sorted(root.rglob("*.json")):
            try:
                payload = read_json_object(json_path)
            except Exception:
                continue
            full_name = str(payload.get("table_fullname") or "").strip()
            column_names = payload.get("column_names")
            if not full_name or not isinstance(column_names, list) or not column_names:
                continue
            parts = [part.strip() for part in full_name.split(".") if part.strip()]
            if len(parts) < 2:
                continue

            column_types = payload.get("column_types") or []
            descriptions = payload.get("description") or []
            sample_rows = payload.get("sample_rows") or []
            if not isinstance(column_types, list):
                column_types = []
            if not isinstance(descriptions, list):
                descriptions = []
            if not isinstance(sample_rows, list):
                sample_rows = []

            if len(column_types) < len(column_names):
                column_types = list(column_types) + [""] * (len(column_names) - len(column_types))
            if len(descriptions) < len(column_names):
                descriptions = list(descriptions) + [""] * (len(column_names) - len(descriptions))

            key = full_name.casefold()
            if key in tables_by_fullname:
                continue
            tables_by_fullname[key] = TableMetadata(
                full_name=full_name,
                namespace=".".join(parts[:-1]),
                short_name=parts[-1],
                column_names=[str(name) for name in column_names],
                column_types=[str(value or "") for value in column_types[: len(column_names)]],
                descriptions=[str(value or "") for value in descriptions[: len(column_names)]],
                sample_rows=[row for row in sample_rows if isinstance(row, dict)],
                snapshot_path=str(json_path),
                table_description=str(
                    payload.get("table_description")
                    or payload.get("description_text")
                    or ""
                ).strip(),
            )

    tables = sorted(tables_by_fullname.values(), key=lambda item: item.full_name)
    if not tables:
        raise ValueError(f"No usable table JSON files found for db_id `{db_id}`.")
    return tables


def build_table_groups(tables: list[TableMetadata]) -> list[TableGroup]:
    grouped: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[TableMetadata]] = defaultdict(list)
    for table in tables:
        grouped[build_group_key(table)].append(table)

    results: list[TableGroup] = []
    for index, (key, members) in enumerate(sorted(grouped.items(), key=lambda item: item[1][0].full_name)):
        members = sorted(members, key=lambda item: item.full_name)
        representative = members[0]
        namespace, normalized_short_name, _ = key
        signature_hash = column_signature_hash(
            representative.column_names,
            representative.column_types,
        )
        group_id = f"group_{index + 1:04d}_{safe_file_stem(representative.full_name)}"
        results.append(
            TableGroup(
                group_id=group_id,
                representative=representative,
                members=members,
                group_key={
                    "namespace": namespace,
                    "normalized_table_name": normalized_short_name,
                    "column_signature_hash": signature_hash,
                },
            )
        )
    return results


def table_metadata_to_prompt_snapshot(
    table: TableMetadata,
    *,
    sample_values_per_column: int,
) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for column_name, column_type, description in zip(
        table.column_names,
        table.column_types,
        table.descriptions,
    ):
        columns.append(
            {
                "column_fullname": f"{table.full_name}.{column_name}",
                "column_name": column_name,
                "data_type": column_type,
                "description": description,
                "sample_values": sample_column_values(
                    table.sample_rows,
                    column_name,
                    limit=sample_values_per_column,
                ),
            }
        )

    return {
        "table_fullname": table.full_name,
        "table_name": table.short_name,
        "description": table.table_description,
        "snapshot_path": table.snapshot_path,
        "columns": columns,
        "sample_rows": table.sample_rows[:sample_values_per_column],
    }


def serialize_table_member(table: TableMetadata) -> dict[str, str]:
    return {
        "table_fullname": table.full_name,
        "namespace": table.namespace,
        "table_name": table.short_name,
        "snapshot_path": table.snapshot_path,
    }


def build_group_prompt_snapshot(
    group: TableGroup,
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> dict[str, Any]:
    representative_snapshot = table_metadata_to_prompt_snapshot(
        group.representative,
        sample_values_per_column=sample_values_per_column,
    )
    representative_snapshot = sanitize_table_snapshot(
        representative_snapshot,
        sample_row_limit=sample_row_limit,
        sample_values_per_column=sample_values_per_column,
        sample_value_max_chars=sample_value_max_chars,
    )
    representative_snapshot["table_group"] = {
        "group_id": group.group_id,
        "grouping_method": GROUPING_METHOD,
        "group_key": group.group_key,
        "representative_table": group.representative.full_name,
        "family_size": len(group.members),
        "member_tables": [serialize_table_member(member) for member in group.members],
    }
    return representative_snapshot


def serialize_group(group: TableGroup) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "grouping_method": GROUPING_METHOD,
        "group_key": group.group_key,
        "representative_table": group.representative.full_name,
        "member_tables": [member.full_name for member in group.members],
        "family_size": len(group.members),
    }


def group_payloads_from_groups(
    groups: list[TableGroup],
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for group in groups:
        payloads.append(
            {
                "group_id": group.group_id,
                "table_fullname": group.representative.full_name,
                "target_table": build_group_prompt_snapshot(
                    group,
                    sample_row_limit=sample_row_limit,
                    sample_values_per_column=sample_values_per_column,
                    sample_value_max_chars=sample_value_max_chars,
                ),
                "linked_columns": [],
                "group": serialize_group(group),
            }
        )
    return payloads


def filter_groups_by_table_fullname(
    groups: list[TableGroup],
    *,
    table_filters: set[str],
) -> list[TableGroup]:
    if not table_filters:
        return groups
    selected: list[TableGroup] = []
    for group in groups:
        member_names = {member.full_name.casefold() for member in group.members}
        if member_names & table_filters:
            selected.append(group)
    return selected


def normalize_selected_member_tables(profile: dict[str, Any], group_payload: dict[str, Any]) -> list[str]:
    group = group_payload.get("group") if isinstance(group_payload.get("group"), dict) else {}
    member_tables = unique_nonempty_strings(group.get("member_tables"))
    member_lookup = {table.casefold(): table for table in member_tables}
    selected = unique_nonempty_strings(
        profile.get("selected_member_tables")
        or profile.get("linked_member_tables")
        or profile.get("member_tables")
    )
    resolved = [
        member_lookup[item.casefold()]
        for item in selected
        if item.casefold() in member_lookup
    ]
    return resolved or member_tables


def resolve_group_column_fullnames(
    values: list[str],
    *,
    group_payload: dict[str, Any],
    selected_member_tables: list[str],
) -> list[str]:
    target_table = group_payload.get("target_table")
    if not isinstance(target_table, dict):
        return []
    columns = ensure_dict_list(target_table.get("columns"))
    available_by_name: dict[str, str] = {}
    available_fullnames: dict[str, str] = {}
    for column in columns:
        column_name = str(column.get("column_name") or "").strip()
        column_fullname = str(column.get("column_fullname") or "").strip()
        if column_name:
            available_by_name[column_name.casefold()] = column_name
        if column_fullname:
            available_fullnames[column_fullname.casefold()] = column_fullname

    member_lookup = {table.casefold(): table for table in selected_member_tables}
    resolved: list[str] = []
    seen: set[str] = set()

    def add_column(table_fullname: str, column_name: str) -> None:
        if not table_fullname or not column_name:
            return
        value = f"{table_fullname}.{column_name}"
        if value in seen:
            return
        resolved.append(value)
        seen.add(value)

    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        source_table = table_fullname_from_column(text)
        if source_table:
            column_name = column_name_from_fullname(text)
            known_column_name = available_by_name.get(column_name.casefold(), column_name)
            if text.casefold() in available_fullnames:
                for member_table in selected_member_tables:
                    add_column(member_table, known_column_name)
                continue
            if source_table.casefold() in member_lookup:
                add_column(member_lookup[source_table.casefold()], known_column_name)
                continue
            continue

        column_name = available_by_name.get(text.casefold())
        if not column_name:
            continue
        for member_table in selected_member_tables:
            add_column(member_table, column_name)

    return resolved


def collect_group_schema_selection(
    *,
    group_payloads: list[dict[str, Any]],
    group_profiles: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    linked_tables: list[str] = []
    linked_columns: list[str] = []
    seen_tables: set[str] = set()
    seen_columns: set[str] = set()

    def add_table(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_tables:
            return
        linked_tables.append(text)
        seen_tables.add(text)

    def add_column(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_columns:
            return
        linked_columns.append(text)
        seen_columns.add(text)
        table_fullname = table_fullname_from_column(text)
        if table_fullname:
            add_table(table_fullname)

    for group_payload, profile in zip(group_payloads, group_profiles):
        if not profile_is_relevant_supportive(profile):
            continue
        selected_member_tables = normalize_selected_member_tables(profile, group_payload)
        for table_fullname in selected_member_tables:
            add_table(table_fullname)

        evidence_columns: list[str] = []
        evidence_columns.extend(unique_nonempty_strings(profile.get("linked_columns")))
        evidence_columns.extend(unique_nonempty_strings(profile.get("other_columns")))
        for unit in ensure_dict_list(profile.get("semantic_units")):
            evidence_columns.extend(collect_evidence_columns_from_unit(unit))
        for column_fullname in resolve_group_column_fullnames(
            unique_nonempty_strings(evidence_columns),
            group_payload=group_payload,
            selected_member_tables=selected_member_tables,
        ):
            add_column(column_fullname)

    return linked_tables, linked_columns


def normalize_sy_schema_linking_name(value: str, db_id: str) -> str:
    text = str(value or "").strip()
    normalized_db_id = str(db_id or "").strip()
    if not normalized_db_id.casefold().startswith("sy_"):
        return text

    prefix = f"{normalized_db_id}."
    if text.casefold().startswith(prefix.casefold()):
        return text[len(prefix):]
    return text


def normalize_schema_linking_names_for_output(
    values: list[str],
    *,
    db_id: str,
) -> list[str]:
    normalized_values: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized_value = normalize_sy_schema_linking_name(value, db_id)
        key = normalized_value.casefold()
        if not normalized_value or key in seen:
            continue
        normalized_values.append(normalized_value)
        seen.add(key)
    return normalized_values


def profile_is_relevant_supportive(profile: dict[str, Any]) -> bool:
    value = profile.get("is_relevant_supportive")
    if isinstance(value, bool):
        return value
    return bool(profile.get("is_relevant"))


def build_compact_semantic_output(group_profiles: list[dict[str, Any]]) -> dict[str, Any]:
    semantics_units: list[dict[str, Any]] = []
    relevant_table_entities: list[dict[str, Any]] = []
    table_other_columns: list[dict[str, Any]] = []
    seen_unit_signatures: set[str] = set()

    for profile in group_profiles:
        if not profile_is_relevant_supportive(profile):
            continue
        representative_table = str(profile.get("representative_table") or "").strip()
        profile_units = ensure_dict_list(profile.get("semantic_units"))
        other_columns = unique_nonempty_strings(profile.get("other_columns"))
        entity_units = [
            unit
            for unit in profile_units
            if str(unit.get("unit_type") or "").strip().casefold() == "entity"
        ]
        relevant_table_entities.append(
            {
                "table_fullname": representative_table,
                "entity_definitions": entity_units,
                "other_columns": other_columns,
            }
        )
        if other_columns:
            table_other_columns.append(
                {
                    "table_fullname": representative_table,
                    "other_columns": other_columns,
                }
            )
        for unit in profile_units:
            compact_unit = {
                "unit_name": str(unit.get("unit_name") or "").strip(),
                "unit_type": str(unit.get("unit_type") or "").strip(),
                "desc": str(unit.get("desc") or "").strip(),
                "grain": str(unit.get("grain") or "").strip(),
            }
            if compact_unit["unit_type"].casefold() == "relationship":
                compact_unit["participants"] = ensure_dict_list(unit.get("participants"))
            try:
                signature = json.dumps(compact_unit, ensure_ascii=False, sort_keys=True)
            except Exception:
                signature = str(compact_unit)
            if signature in seen_unit_signatures:
                continue
            semantics_units.append(compact_unit)
            seen_unit_signatures.add(signature)

    return {
        "semantics_units": semantics_units,
        "relevant_table_entities": relevant_table_entities,
        "table_other_columns": table_other_columns,
    }


def first_nonempty_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def normalize_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text in {"true", "yes", "y", "1", "relevant", "linked", "相关"}:
        return True
    if text in {"false", "no", "n", "0", "irrelevant", "unlinked", "不相关"}:
        return False
    return None


def nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def extract_group_response_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "table_group_profile",
        "group_profile",
        "database_table_semantic_sketch",
        "profile",
        "result",
    ):
        value = parsed.get(key)
        if isinstance(value, dict):
            merged = dict(value)
            for metadata_key in (
                "group_id",
                "representative_table",
                "is_relevant_supportive",
                "is_relevant",
                "member_table_scope",
                "selected_member_tables",
                "linked_member_tables",
                "other_columns",
            ):
                if metadata_key in parsed and metadata_key not in merged:
                    merged[metadata_key] = parsed[metadata_key]
            return merged
    return parsed


def collect_response_strings(payload: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        values.extend(unique_nonempty_strings(payload.get(key)))
    return unique_nonempty_strings(values)


def collect_column_references(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return unique_nonempty_strings(value)
    if not isinstance(value, list):
        return []

    columns: list[str] = []
    for item in value:
        if isinstance(item, str):
            columns.append(item)
            continue
        if not isinstance(item, dict):
            continue
        columns.extend(
            unique_nonempty_strings(
                [
                    item.get("column_fullname"),
                    item.get("column_name"),
                    item.get("name"),
                    item.get("field_name"),
                ]
            )
        )
        columns.extend(unique_nonempty_strings(item.get("evidence_columns")))
    return unique_nonempty_strings(columns)


def normalize_database_group_profile(
    *,
    parsed: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    payload = extract_group_response_payload(parsed)
    group = item.get("group") if isinstance(item.get("group"), dict) else {}
    representative_table = str(
        group.get("representative_table") or item.get("table_fullname") or ""
    ).strip()

    schema_linking_payload = nested_dict(payload, "schema_linking")
    table_selection_payload = nested_dict(payload, "table_selection")
    table_group_payload = nested_dict(payload, "table_group")

    profile_input = dict(payload)
    raw_relevance = payload.get("is_relevant_supportive")
    if raw_relevance is None:
        raw_relevance = payload.get("is_relevant")
    normalized_relevance = normalize_bool(raw_relevance)
    if normalized_relevance is not None:
        profile_input["is_relevant_supportive"] = normalized_relevance
        profile_input["is_relevant"] = normalized_relevance

    profile = normalize_model_profile(
        parsed=profile_input,
        table_fullname=representative_table,
        target_table=item["target_table"],
    )

    member_table_scope = first_nonempty_string(
        payload.get("member_table_scope"),
        payload.get("table_scope"),
        table_selection_payload.get("member_table_scope"),
        table_selection_payload.get("table_scope"),
        "all_members",
    )
    selected_member_tables = collect_response_strings(
        payload,
        "selected_member_tables",
        "linked_member_tables",
    )
    selected_member_tables.extend(
        collect_response_strings(
            table_selection_payload,
            "selected_member_tables",
            "linked_member_tables",
        )
    )
    linked_columns = collect_response_strings(payload, "linked_columns", "evidence_columns")
    linked_columns.extend(
        collect_response_strings(
            schema_linking_payload,
            "linked_columns",
            "evidence_columns",
            "columns",
        )
    )
    other_columns = collect_column_references(payload.get("other_columns"))

    if member_table_scope.casefold() == "none" or not bool(profile.get("is_relevant_supportive")):
        member_table_scope = "none"
        profile["is_relevant_supportive"] = False
        profile["is_relevant"] = False
        selected_member_tables = []
        linked_columns = []
        other_columns = []
        profile["semantic_units"] = []

    profile.update(
        {
            "group_id": first_nonempty_string(
                payload.get("group_id"),
                table_group_payload.get("group_id"),
                group.get("group_id"),
                item.get("group_id"),
            ),
            "grouping_method": GROUPING_METHOD,
            "representative_table": first_nonempty_string(
                payload.get("representative_table"),
                table_group_payload.get("representative_table"),
                representative_table,
            ),
            "member_tables": unique_nonempty_strings(group.get("member_tables")),
            "family_size": int(group.get("family_size") or 0),
            "member_table_scope": member_table_scope,
            "selected_member_tables": unique_nonempty_strings(selected_member_tables),
            "linked_columns": unique_nonempty_strings(linked_columns),
            "other_columns": unique_nonempty_strings(other_columns),
            "prompt_path": item.get("prompt_path"),
            "response_path": item.get("response_path"),
        }
    )
    return profile


class DatabaseTableSemanticSketchRunner:
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
        self.reasoning_mode = reasoning_mode
        self.database_root = database_root
        self.spider2_root = spider2_root
        self.sample_values_per_column = sample_values_per_column
        self.dry_run = dry_run
        self.group_prompt_dir = self.log_dir / "group_prompts"
        self.group_response_dir = self.log_dir / "group_responses"
        for directory in (self.log_dir, self.group_prompt_dir, self.group_response_dir):
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
                "target_table_group",
            ],
        )

    def build_prompt(
        self,
        *,
        context: dict[str, str],
        target_table: dict[str, Any],
    ) -> str:
        prompt_target_table = dict(target_table)
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
                "target_table_group": prompt_target_table,
                "target_table": prompt_target_table,
            },
        )

    def prepare_group_prompts(
        self,
        *,
        context: dict[str, str],
        group_payloads: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        prompts: list[str] = []
        items: list[dict[str, Any]] = []
        for item in group_payloads:
            group = item.get("group") if isinstance(item.get("group"), dict) else {}
            group_id = str(item.get("group_id") or group.get("group_id") or "").strip()
            prompt = self.build_prompt(
                context=context,
                target_table=item["target_table"],
            )
            stem = safe_file_stem(group_id or str(item.get("table_fullname") or "group"))
            prompt_path = self.group_prompt_dir / f"{stem}.md"
            response_path = self.group_response_dir / f"{stem}.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            item = {
                **item,
                "prompt_path": str(prompt_path),
                "response_path": str(response_path),
            }
            items.append(item)
            prompts.append(prompt)
        return items, prompts

    @staticmethod
    def build_dry_run_group_profile(item: dict[str, Any]) -> dict[str, Any]:
        target_table = item.get("target_table") if isinstance(item.get("target_table"), dict) else {}
        group = item.get("group") if isinstance(item.get("group"), dict) else {}
        representative_table = str(group.get("representative_table") or item.get("table_fullname") or "").strip()
        member_tables = unique_nonempty_strings(group.get("member_tables")) or [representative_table]
        attributes = []
        for column in ensure_dict_list(target_table.get("columns")):
            column_name = str(column.get("column_name") or "").strip()
            column_fullname = str(column.get("column_fullname") or "").strip()
            if not column_name:
                continue
            attributes.append(
                {
                    "name": column_name,
                    "semantics": "Dry-run placeholder attribute support.",
                    "evidence_columns": [column_fullname or column_name],
                }
            )
        return {
            "group_id": str(group.get("group_id") or item.get("group_id") or "").strip(),
            "grouping_method": GROUPING_METHOD,
            "representative_table": representative_table,
            "member_tables": member_tables,
            "family_size": int(group.get("family_size") or len(member_tables)),
            "table_fullname": representative_table,
            "supported_question_semantics": [
                "Dry-run placeholder: inspect the generated prompt for intended group-level support."
            ] if attributes else [],
            "is_relevant_supportive": bool(attributes),
            "is_relevant": bool(attributes),
            "other_columns": [],
            "semantic_units": [
                {
                    "unit_type": "entity",
                    "unit_name": table_name_from_fullname(representative_table) or representative_table,
                    "desc": "Dry-run placeholder semantic unit.",
                    "grain": "Dry-run placeholder grain.",
                    "attributes": attributes,
                    "participants": [],
                }
            ] if attributes else [],
            "prompt_path": item.get("prompt_path"),
            "response_path": item.get("response_path"),
        }

    def parse_group_responses(
        self,
        *,
        items: list[dict[str, Any]],
        raw_responses: list[str],
    ) -> list[dict[str, Any]]:
        profiles: list[dict[str, Any]] = []
        for item, raw_response in zip(items, raw_responses):
            response_path = Path(item["response_path"])
            response_path.write_text(raw_response or "", encoding="utf-8")
            group = item.get("group") if isinstance(item.get("group"), dict) else {}
            representative_table = str(group.get("representative_table") or item.get("table_fullname") or "").strip()
            if not raw_response or not raw_response.strip():
                profiles.append(
                    self.build_error_group_profile(
                        item=item,
                        error="LLM returned empty response.",
                    )
                )
                continue
            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                profiles.append(
                    self.build_error_group_profile(
                        item=item,
                        error=f"Failed to parse database table semantic sketch JSON: {exc}",
                    )
                )
                continue
            profile = normalize_database_group_profile(parsed=parsed, item=item)
            payload = extract_group_response_payload(parsed)
            if "semantic_units" not in payload and "units" not in payload:
                profile["warning"] = "`semantic_units` was missing or not a list."
            profiles.append(profile)
        return profiles

    @staticmethod
    def build_error_group_profile(
        *,
        item: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        group = item.get("group") if isinstance(item.get("group"), dict) else {}
        representative_table = str(group.get("representative_table") or item.get("table_fullname") or "").strip()
        member_tables = unique_nonempty_strings(group.get("member_tables"))
        return {
            "group_id": str(group.get("group_id") or item.get("group_id") or "").strip(),
            "grouping_method": GROUPING_METHOD,
            "representative_table": representative_table,
            "member_tables": member_tables,
            "family_size": int(group.get("family_size") or len(member_tables)),
            "table_fullname": representative_table,
            "supported_question_semantics": [],
            "is_relevant_supportive": False,
            "is_relevant": False,
            "other_columns": [],
            "semantic_units": [],
            "error": error,
            "prompt_path": item.get("prompt_path"),
            "response_path": item.get("response_path"),
        }

    def analyze_groups(
        self,
        *,
        context: dict[str, str],
        group_payloads: list[dict[str, Any]],
        max_concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        items, prompts = self.prepare_group_prompts(
            context=context,
            group_payloads=group_payloads,
        )
        if self.dry_run:
            return [self.build_dry_run_group_profile(item) for item in items]
        if self.llm is None:
            raise RuntimeError("LLM client is not initialized. Disable --dry-run to call the LLM.")

        raw_responses = self.llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(prompts) > 1,
            progress_desc="Database table semantic sketch",
        )
        return self.parse_group_responses(items=items, raw_responses=raw_responses)

    def prepare_case(
        self,
        *,
        case_input: DatabaseCaseInput,
        args: argparse.Namespace,
    ) -> PreparedDatabaseCase:
        started_at = time.perf_counter()
        context = normalize_context(case_input=case_input, args=args)
        if not context.get("user_intent"):
            raise ValueError("Question text is required. Provide --question or an input.json question.")
        if not context.get("db_id"):
            raise ValueError("db_id is required. Provide --db-id or include it in inputs.")

        tables = load_database_tables(
            db_id=context["db_id"],
            database_root=self.database_root,
            spider2_root=self.spider2_root,
        )
        table_groups = build_table_groups(tables)
        table_groups = filter_groups_by_table_fullname(
            table_groups,
            table_filters=normalize_casefold_set(args.table_fullname),
        )
        if not table_groups:
            raise ValueError("No table groups were found to analyze.")

        group_payloads = group_payloads_from_groups(
            table_groups,
            sample_row_limit=args.sample_row_limit,
            sample_values_per_column=self.sample_values_per_column,
            sample_value_max_chars=args.sample_value_max_chars,
        )
        return PreparedDatabaseCase(
            case_input=case_input,
            runner=self,
            context=context,
            table_groups=table_groups,
            group_payloads=group_payloads,
            started_at=started_at,
        )

    def finalize_case(
        self,
        *,
        prepared: PreparedDatabaseCase,
        group_profiles: list[dict[str, Any]],
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        linked_tables, linked_columns = collect_group_schema_selection(
            group_payloads=prepared.group_payloads,
            group_profiles=group_profiles,
        )
        output_path = database_case_output_path(
            case_input=prepared.case_input,
            log_dir=self.log_dir,
            output_filename=args.output_filename,
        )
        ok = all(not profile.get("error") for profile in group_profiles)
        compact_output = build_compact_semantic_output(group_profiles)
        output_payload = dict(compact_output)
        write_json(output_path, output_payload)
        write_json(self.log_dir / "output.json", output_payload)

        if bool(getattr(args, "write_schema_linking", False)):
            output_linked_tables = normalize_schema_linking_names_for_output(
                linked_tables,
                db_id=prepared.context.get("db_id", ""),
            )
            output_linked_columns = normalize_schema_linking_names_for_output(
                linked_columns,
                db_id=prepared.context.get("db_id", ""),
            )
            schema_linking_payload = build_schema_linking_with_overall_result(
                schema_linking_payload={},
                context=prepared.context,
                linked_tables=output_linked_tables,
                linked_columns=output_linked_columns,
                source=OUTPUT_SOURCE,
            )
            schema_linking_output_path = (
                output_path.parent
                / str(getattr(args, "schema_linking_output_filename", DEFAULT_SCHEMA_LINKING_OUTPUT_FILENAME))
            ).resolve()
            write_json(schema_linking_output_path, schema_linking_payload)
            write_json(self.log_dir / "schema_linking.json", schema_linking_payload)

        return {
            "ok": ok,
            "question_id": prepared.context.get("question_id", ""),
            "db_id": prepared.context.get("db_id", ""),
            "group_count": len(prepared.table_groups),
            "relevant_group_count": sum(
                1 for profile in group_profiles if profile_is_relevant_supportive(profile)
            ),
            "output_path": str(output_path),
            "log_dir": str(self.log_dir),
        }

    def run_case(
        self,
        *,
        case_input: DatabaseCaseInput,
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        prepared = self.prepare_case(case_input=case_input, args=args)
        sketch_started_at = time.perf_counter()
        requested_concurrency = args.max_table_concurrency or args.max_workers
        group_profiles = self.analyze_groups(
            context=prepared.context,
            group_payloads=prepared.group_payloads,
            max_concurrency=resolve_table_sketch_concurrency(requested_concurrency),
        )
        emit_step_done_log(
            prefix="DATABASE_TABLE_SEMANTIC_SKETCH",
            step="group_analysis",
            elapsed_seconds=time.perf_counter() - sketch_started_at,
            groups=len(group_profiles),
            dry_run=bool(self.dry_run),
        )
        return self.finalize_case(
            prepared=prepared,
            group_profiles=group_profiles,
            args=args,
        )


def run_single_case(
    *,
    case_input: DatabaseCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    runner = DatabaseTableSemanticSketchRunner(
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
    output_path = database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    summary = {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id", ""),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "group_count": int(payload.get("group_count") or 0),
        "relevant_group_count": int(payload.get("relevant_group_count") or 0),
        "dry_run": bool(args.dry_run),
    }
    return summary


def run_single_case_safe(
    *,
    case_input: DatabaseCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    output_path = database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
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
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "dry_run": bool(args.dry_run),
        }
        write_json(log_dir / "error.json", failure_payload)
        write_json(output_path, failure_payload)
        return failure_payload


def prepare_single_case_for_batch_safe(
    *,
    case_input: DatabaseCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> PreparedDatabaseCase | dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    output_path = database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    try:
        runner = DatabaseTableSemanticSketchRunner(
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
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
            "dry_run": bool(args.dry_run),
        }
        write_json(log_dir / "error.json", failure_payload)
        write_json(output_path, failure_payload)
        return failure_payload


def summarize_database_case_payload(
    *,
    payload: dict[str, Any],
    case_input: DatabaseCaseInput,
    log_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_path = database_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    summary = {
        "ok": bool(payload.get("ok")),
        "question_id": payload.get("question_id", ""),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "group_count": int(payload.get("group_count") or 0),
        "relevant_group_count": int(payload.get("relevant_group_count") or 0),
        "dry_run": bool(args.dry_run),
    }
    return summary


def finalize_prepared_database_case(
    *,
    prepared: PreparedDatabaseCase,
    group_profiles: list[dict[str, Any]],
    args: argparse.Namespace,
    sketch_elapsed_seconds: float,
) -> dict[str, Any]:
    emit_step_done_log(
        prefix="DATABASE_TABLE_SEMANTIC_SKETCH",
        step="group_analysis",
        elapsed_seconds=sketch_elapsed_seconds,
        groups=len(group_profiles),
        dry_run=bool(prepared.runner.dry_run),
    )
    payload = prepared.runner.finalize_case(
        prepared=prepared,
        group_profiles=group_profiles,
        args=args,
    )
    return summarize_database_case_payload(
        payload=payload,
        case_input=prepared.case_input,
        log_dir=prepared.runner.log_dir,
        args=args,
    )


# Compatibility exports for callers that still import algorithm helpers from
# this run module. Implementations live in er2data/nl2er modules.
TableMetadata = ComponentTableMetadata
TableGroup = ComponentTableGroup
clear_tb = component_clear_tb
remove_digits = component_remove_digits
normalize_casefold_set = component_normalize_casefold_set
normalized_column_signature = component_normalized_column_signature
column_signature_hash = component_column_signature_hash
build_group_key = component_build_group_key
load_database_tables = component_load_database_tables
table_metadata_to_prompt_snapshot = component_table_metadata_to_prompt_snapshot
build_table_groups = component_build_table_groups
serialize_table_member = component_serialize_table_member
build_group_prompt_snapshot = component_build_group_prompt_snapshot
serialize_group = component_serialize_group
group_payloads_from_groups = component_group_payloads_from_groups
filter_groups_by_table_fullname = component_filter_groups_by_table_fullname
normalize_selected_member_tables = component_normalize_selected_member_tables
resolve_group_column_fullnames = component_resolve_group_column_fullnames
collect_group_schema_selection = component_collect_group_schema_selection
profile_is_relevant_supportive = component_profile_is_relevant_supportive
build_compact_semantic_output = component_build_compact_semantic_output
first_nonempty_string = component_first_nonempty_string
normalize_bool = component_normalize_bool
nested_dict = component_nested_dict
extract_group_response_payload = component_extract_group_response_payload
collect_response_strings = component_collect_response_strings
collect_column_references = component_collect_column_references
normalize_database_group_profile = component_normalize_database_group_profile


def main() -> None:
    args = parse_args()
    requested_question_ids = normalize_name_filters(args.question_id)
    batch_root, case_inputs = discover_database_case_inputs(
        input_path=args.input_path,
        metadata_dir=args.metadata_dir,
        output_path=args.output_path,
        output_filename=args.output_filename,
        requested_question_ids=requested_question_ids,
    )
    case_inputs = filter_cases_by_question_id(
        cases=case_inputs,
        requested_question_ids=requested_question_ids,
    )
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()

    print(f"[DATABASE_TABLE_SEMANTIC_SKETCH] batch_root={batch_root}")
    print(f"[DATABASE_TABLE_SEMANTIC_SKETCH] discovered_cases={len(case_inputs)}")
    print(f"[DATABASE_TABLE_SEMANTIC_SKETCH] run_timestamp={run_timestamp}")
    print(f"[DATABASE_TABLE_SEMANTIC_SKETCH] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[DATABASE_TABLE_SEMANTIC_SKETCH] log_root={resolve_path(args.log_root)}")
    print(f"[DATABASE_TABLE_SEMANTIC_SKETCH] dry_run={bool(args.dry_run)}")

    completed_summaries: list[dict[str, Any]] = []
    prepared_cases: list[PreparedDatabaseCase] = []
    for case_input in case_inputs:
        prepared_or_failure = prepare_single_case_for_batch_safe(
            case_input=case_input,
            run_timestamp=run_timestamp,
            args=args,
        )
        if isinstance(prepared_or_failure, PreparedDatabaseCase):
            prepared_cases.append(prepared_or_failure)
        else:
            completed_summaries.append(prepared_or_failure)

    global_prompts: list[str] = []
    case_items: list[list[dict[str, Any]]] = []
    case_raw_responses: list[list[str]] = [[] for _ in prepared_cases]
    item_case_indexes: list[int] = []

    for case_index, prepared in enumerate(prepared_cases):
        items, prompts = prepared.runner.prepare_group_prompts(
            context=prepared.context,
            group_payloads=prepared.group_payloads,
        )
        case_items.append(items)
        for prompt in prompts:
            global_prompts.append(prompt)
            item_case_indexes.append(case_index)

    print(
        "[DATABASE_TABLE_SEMANTIC_SKETCH] prepared "
        f"cases={len(prepared_cases)} "
        f"failed_cases={len(completed_summaries)} "
        f"group_prompt_count={len(global_prompts)}"
    )

    sketch_started_at = time.perf_counter()
    if args.dry_run:
        for case_index, _prepared in enumerate(prepared_cases):
            case_raw_responses[case_index] = []
    elif global_prompts:
        requested_concurrency = args.max_table_concurrency or args.max_workers
        max_concurrency = resolve_table_sketch_concurrency(requested_concurrency)
        print(
            "[DATABASE_TABLE_SEMANTIC_SKETCH] global_group_sketch_batch "
            f"case_count={len(prepared_cases)} "
            f"group_prompt_count={len(global_prompts)} "
            f"max_concurrency={max_concurrency}"
        )
        settings = load_settings()
        config = apply_reasoning_mode(settings.llm.get(args.model_config), args.reasoning_mode)
        llm = LLMClient(config)
        raw_responses = llm.batch_single_turn(
            global_prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(global_prompts) > 1,
            progress_desc="Database table semantic sketch",
        )
        for case_index, raw_response in zip(item_case_indexes, raw_responses):
            case_raw_responses[case_index].append(raw_response)

    sketch_elapsed_seconds = time.perf_counter() - sketch_started_at
    for case_index, prepared in enumerate(prepared_cases):
        if args.dry_run:
            group_profiles = [
                prepared.runner.build_dry_run_group_profile(item)
                for item in case_items[case_index]
            ]
        else:
            group_profiles = prepared.runner.parse_group_responses(
                items=case_items[case_index],
                raw_responses=case_raw_responses[case_index],
            )
        summary = finalize_prepared_database_case(
            prepared=prepared,
            group_profiles=group_profiles,
            args=args,
            sketch_elapsed_seconds=sketch_elapsed_seconds,
        )
        completed_summaries.append(summary)

    success_count = sum(1 for item in completed_summaries if item.get("ok"))
    print(
        "[DATABASE_TABLE_SEMANTIC_SKETCH] finalized "
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
        prefix="DATABASE_TABLE_SEMANTIC_SKETCH",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
