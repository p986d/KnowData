from __future__ import annotations

import argparse
import copy
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.llm.reasoning import REASONING_MODE_MAP
from src.nl2sql.base import NL2SQLRequest
from src.nl2sql.database_profile import resolve_database_execution_profile
from src.nl2sql.defaults import (
    DEFAULT_ENGINE_SCRIPT,
    DEFAULT_REFORCE_ROOT,
)
from src.nl2sql.registry import get_engine_provider, resolve_engine_runtime
from src.er2data.er2query import (
    DEFAULT_ER2QUERY_TEMPLATE_NAME,
    DEFAULT_PROMPT_DIR,
    ER2DataRunner,
    normalize_er_input_payload,
    read_external_knowledge_from_sidecar,
    read_json,
    read_question_id,
    read_sidecar_context,
    resolve_db_hint,
    resolve_er2query_prompt_context,
    safe_file_stem,
    sanitize_nl2sql_response,
    shorten_text,
    write_json,
)
from src.utils.sqlglot_parser import SqlglotParser
from src.utils.run_log import build_timestamp, emit_step_done_log


DEFAULT_METADATA_DIR = Path("src/metadata")
DEFAULT_LOG_ROOT = Path("log/er2data_2")
DEFAULT_INPUT_FILENAME = "nl2er_output.json"
DEFAULT_DIFF_INPUT_FILENAME = "nl2er_output_diff.json"
DEFAULT_OUTPUT_FILENAME = "sql_candidates.json"
DEFAULT_SCHEMA_LINKING_FILENAME = "schema_linking.json"
SQL_CANDIDATE_TARGET_UNIT_TYPES = {"entity", "relationship"}


@dataclass(slots=True)
class CaseInput:
    input_path: Path
    case_dir: Path
    relative_case_dir: Path


def parse_cli_bool(value: str) -> bool:
    normalized = str(value or "").strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError(f"Expected `true` or `false`, got `{value}`.")


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"Expected a positive integer, got `{value}`."
        ) from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.")
    return parsed


def resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    return resolved


def discover_case_inputs(metadata_dir: str | Path) -> tuple[Path, list[CaseInput]]:
    root_dir = resolve_path(metadata_dir)
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata path does not exist: {root_dir}")

    if root_dir.is_file():
        if root_dir.name != DEFAULT_INPUT_FILENAME:
            raise ValueError(
                f"Expected `{DEFAULT_INPUT_FILENAME}` when a file path is provided, got `{root_dir.name}`."
            )
        return root_dir.parent, [
            CaseInput(
                input_path=root_dir,
                case_dir=root_dir.parent,
                relative_case_dir=Path(root_dir.parent.name),
            )
        ]

    direct_input = root_dir / DEFAULT_INPUT_FILENAME
    if direct_input.exists():
        return root_dir, [
            CaseInput(
                input_path=direct_input.resolve(),
                case_dir=root_dir,
                relative_case_dir=Path(root_dir.name),
            )
        ]

    input_paths = sorted({path.resolve() for path in root_dir.rglob(DEFAULT_INPUT_FILENAME)})
    if not input_paths:
        raise FileNotFoundError(
            f"No `{DEFAULT_INPUT_FILENAME}` files were found under {root_dir}"
        )

    case_inputs: list[CaseInput] = []
    for input_path in input_paths:
        case_dir = input_path.parent
        case_inputs.append(
            CaseInput(
                input_path=input_path,
                case_dir=case_dir,
                relative_case_dir=case_dir.relative_to(root_dir),
            )
        )
    return root_dir, case_inputs


def extract_fullnames(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    fullnames: list[str] = []
    for item in items:
        if isinstance(item, dict):
            fullname = str(
                item.get("fullname") or item.get("full_name") or item.get("name") or ""
            ).strip()
        else:
            fullname = str(item or "").strip()
        if fullname:
            fullnames.append(fullname)
    return fullnames


def load_optional_diff_er_model(
    *,
    input_path: str | Path,
    diff_filename: str,
) -> tuple[dict[str, Any] | None, str | None]:
    resolved_input = Path(input_path)
    resolved_diff_filename = str(diff_filename or "").strip()
    if not resolved_diff_filename:
        return None, None
    diff_path = resolved_input.with_name(resolved_diff_filename)
    if not diff_path.exists():
        return None, str(diff_path)
    diff_payload = read_json(diff_path)
    diff_model = normalize_er_input_payload(
        diff_payload,
        input_path=diff_path,
        merge_connections_into_relationships=False,
    )
    return diff_model, str(diff_path)


def _normalize_concept_name(value: Any) -> str:
    return str(value or "").strip().casefold()


def _unit_concept_name(unit: Any) -> str:
    target_unit = getattr(unit, "target_unit", {})
    if isinstance(target_unit, dict):
        name = str(target_unit.get("name") or "").strip()
        if name:
            return name
    return str(getattr(unit, "target_unit_name", "") or "").strip()


def _participant_entity_set(unit: Any) -> tuple[str, ...]:
    target_unit = getattr(unit, "target_unit", {})
    if not isinstance(target_unit, dict):
        return ()
    participants = target_unit.get("participants")
    if not isinstance(participants, list):
        return ()
    return tuple(
        sorted(
            {
                _normalize_concept_name(participant.get("entity"))
                for participant in participants
                if isinstance(participant, dict)
                and _normalize_concept_name(participant.get("entity"))
            }
        )
    )


def _unit_merge_key(unit: Any) -> tuple[Any, ...] | None:
    unit_type = str(getattr(unit, "target_unit_type", "") or "").strip().casefold()
    unit_name = _normalize_concept_name(_unit_concept_name(unit))
    if not unit_type or not unit_name:
        return None
    if unit_type == "entity":
        return (unit_type, unit_name)
    if unit_type in {"relationship", "connection"}:
        return (unit_type, unit_name, _participant_entity_set(unit))
    return None


def _merge_string_list(left: Any, right: Any) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for values in (left, right):
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, str):
                continue
            text = value.strip()
            normalized = text.casefold()
            if not text or normalized in seen:
                continue
            merged.append(text)
            seen.add(normalized)
    return merged


def _attribute_name(attr: dict[str, Any]) -> str:
    for key in ("name", "attr", "attribute_name", "field_name", "key"):
        text = str(attr.get(key) or "").strip()
        if text:
            return text
    return ""


def _merge_attributes(left: Any, right: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}

    for values in (left, right):
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            name = _attribute_name(item)
            normalized = _normalize_concept_name(name)
            if not normalized:
                continue
            if normalized not in index_by_name:
                index_by_name[normalized] = len(merged)
                merged.append(copy.deepcopy(item))
                continue
            existing = merged[index_by_name[normalized]]
            for key, value in item.items():
                if key not in existing or existing.get(key) in (None, "", []):
                    existing[key] = copy.deepcopy(value)
    return merged


def _participant_key(participant: dict[str, Any]) -> str:
    return _normalize_concept_name(participant.get("entity"))


def _merge_participants(left: Any, right: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []

    if isinstance(left, list):
        for item in left:
            if isinstance(item, dict) and _participant_key(item):
                merged.append(copy.deepcopy(item))

    if not isinstance(right, list):
        return merged

    for item in right:
        if not isinstance(item, dict):
            continue
        key = _participant_key(item)
        if not key:
            continue
        matching_indices = [
            index
            for index, existing in enumerate(merged)
            if _participant_key(existing) == key
        ]
        if not matching_indices:
            merged.append(copy.deepcopy(item))
            continue
        for matching_index in matching_indices:
            existing = merged[matching_index]
            existing["identifier_attrs"] = _merge_string_list(
                existing.get("identifier_attrs"),
                item.get("identifier_attrs"),
            )
            for field_name in ("entity", "role", "cardinality"):
                if not str(existing.get(field_name) or "").strip() and str(
                    item.get(field_name) or ""
                ).strip():
                    existing[field_name] = copy.deepcopy(item[field_name])
    return merged


def _merge_conditions(left: Any, right: Any) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for values in (left, right):
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            key = str(
                item.get("condition_name") or item.get("name") or json.dumps(item, sort_keys=True)
            ).casefold()
            if key in seen:
                continue
            merged.append(copy.deepcopy(item))
            seen.add(key)
    return merged


def _merge_target_unit(primary: dict[str, Any], diff: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(primary)
    for key, value in diff.items():
        if key in {"attrs", "attributes", "identifier_attrs", "participants"}:
            continue
        if key not in merged or merged.get(key) in (None, "", []):
            merged[key] = copy.deepcopy(value)

    merged["attrs"] = _merge_attributes(primary.get("attrs"), diff.get("attrs"))
    merged["identifier_attrs"] = _merge_string_list(
        primary.get("identifier_attrs"),
        diff.get("identifier_attrs"),
    )
    if "participants" in primary or "participants" in diff:
        merged["participants"] = _merge_participants(
            primary.get("participants"),
            diff.get("participants"),
        )
    return merged


def _merge_unit_payload(primary_unit: Any, diff_unit: Any) -> None:
    primary_target = getattr(primary_unit, "target_unit", {})
    diff_target = getattr(diff_unit, "target_unit", {})
    if isinstance(primary_target, dict) and isinstance(diff_target, dict):
        primary_unit.target_unit = _merge_target_unit(primary_target, diff_target)

    primary_unit.applied_conditions = _merge_conditions(
        getattr(primary_unit, "applied_conditions", []),
        getattr(diff_unit, "applied_conditions", []),
    )
    primary_unit.condition_selection = _merge_conditions(
        getattr(primary_unit, "condition_selection", []),
        getattr(diff_unit, "condition_selection", []),
    )


def _assign_unique_variant_identity(
    unit: Any,
    *,
    index: int,
    used_unit_ids: set[str],
    used_target_names: set[str],
) -> None:
    base_name = str(getattr(unit, "target_unit_name", "") or "").strip()
    if not base_name:
        base_name = _unit_concept_name(unit) or f"variant_unit_{index + 1}"

    candidate_name = base_name
    suffix = 2
    while candidate_name in used_target_names:
        candidate_name = f"{base_name} [variant-{suffix}]"
        suffix += 1
    unit.target_unit_name = candidate_name
    used_target_names.add(candidate_name)

    base_unit_id = str(getattr(unit, "unit_id", "") or "").strip() or f"variant::{index + 1}"
    candidate_unit_id = base_unit_id
    suffix = 2
    while candidate_unit_id in used_unit_ids:
        candidate_unit_id = f"{base_unit_id}::variant::{suffix}"
        suffix += 1
    unit.unit_id = candidate_unit_id
    used_unit_ids.add(candidate_unit_id)


def merge_primary_and_diff_units(
    primary_units: list[Any],
    diff_units: list[Any],
) -> list[Any]:
    merged_units = list(primary_units)
    used_unit_ids = {str(unit.unit_id) for unit in merged_units}
    used_target_names = {str(unit.target_unit_name) for unit in merged_units}
    unit_by_merge_key: dict[tuple[Any, ...], Any] = {}
    for unit in merged_units:
        merge_key = _unit_merge_key(unit)
        if merge_key is not None:
            unit_by_merge_key.setdefault(merge_key, unit)

    for index, unit in enumerate(diff_units):
        merge_key = _unit_merge_key(unit)
        if merge_key is not None and merge_key in unit_by_merge_key:
            _merge_unit_payload(unit_by_merge_key[merge_key], unit)
            continue

        _assign_unique_variant_identity(
            unit,
            index=index,
            used_unit_ids=used_unit_ids,
            used_target_names=used_target_names,
        )
        merged_units.append(unit)
        if merge_key is not None:
            unit_by_merge_key.setdefault(merge_key, unit)

    return merged_units


def build_candidate_schema_linking_details(
    sql_candidates: list[str],
    *,
    sql_dialect: str | None = None,
) -> tuple[list[dict[str, Any]], set[str], set[str], bool]:
    candidate_details: list[dict[str, Any]] = []
    merged_tables: set[str] = set()
    merged_columns: set[str] = set()
    any_sqlglot_parse = False

    for sql in sql_candidates:
        detail: dict[str, Any] = {
            "sql": sql,
            "linked_tables": [],
            "linked_columns": [],
            "parse_ok": False,
            "parse_error": "",
        }
        try:
            parsed = SqlglotParser.extract_table_columns_and_join_conditions(
                sql,
                dialect=sql_dialect,
            )
            parsed_tables: list[str] = []
            parsed_columns: list[str] = []
            for table_payload in parsed.get("tables", []):
                if not isinstance(table_payload, dict):
                    continue
                table_name = str(table_payload.get("table_name") or "").strip()
                if not table_name:
                    continue
                parsed_tables.append(table_name)
                for column_name in table_payload.get("columns", []):
                    column_text = str(column_name or "").strip()
                    if not column_text:
                        continue
                    parsed_columns.append(f"{table_name}.{column_text}")

            unique_tables = sorted({name for name in parsed_tables if name})
            unique_columns = sorted({name for name in parsed_columns if name})
            detail["linked_tables"] = unique_tables
            detail["linked_columns"] = unique_columns
            detail["parse_ok"] = True
            merged_tables.update(unique_tables)
            merged_columns.update(unique_columns)
            any_sqlglot_parse = True
        except Exception as exc:
            detail["parse_error"] = str(exc)
        candidate_details.append(detail)

    return candidate_details, merged_tables, merged_columns, any_sqlglot_parse


def build_schema_linking_output(
    *,
    db_id: str,
    unit_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}

    sql_dialect: str | None = None
    try:
        sql_dialect = resolve_database_execution_profile(db_id).dialect
    except Exception:
        sql_dialect = None

    for unit_result in unit_results:
        unit_name = (
            str(unit_result.get("target_unit_name") or "").strip()
            or str(unit_result.get("unit_id") or "").strip()
        )
        if not unit_name:
            continue

        candidate_generation = unit_result.get("sql_candidate_generation", {})
        schema_linking = unit_result.get("schema_linking", {})
        question_generation = unit_result.get("question_generation", {})

        sql_candidates: list[str] = []
        if isinstance(candidate_generation, dict):
            raw_candidates = candidate_generation.get("sql_candidates", [])
            if isinstance(raw_candidates, list):
                sql_candidates = [
                    str(candidate or "").strip()
                    for candidate in raw_candidates
                    if str(candidate or "").strip()
                ]

        (
            candidate_details,
            parsed_tables,
            parsed_columns,
            any_sqlglot_parse,
        ) = build_candidate_schema_linking_details(
            sql_candidates,
            sql_dialect=sql_dialect,
        )

        payload: dict[str, Any] = {
            "db_id": db_id,
            "question": str(unit_result.get("question") or ""),
            "linked_tables": sorted(parsed_tables),
            "linked_columns": sorted(parsed_columns),
            "sources": {
                "provider_linking": False,
                "sqlglot_parse": bool(parsed_tables or parsed_columns),
            },
            "candidates": candidate_details,
        }

        error = ""
        if isinstance(question_generation, dict):
            error = str(question_generation.get("error") or "").strip()
        if not error and isinstance(candidate_generation, dict):
            error = str(candidate_generation.get("error") or "").strip()
        if not error and isinstance(schema_linking, dict):
            error = str(schema_linking.get("error") or "").strip()
        if error:
            payload["error"] = error

        output[unit_name] = payload

    return output


def build_compact_sql_candidate_output(
    *,
    db_id: str,
    unit_results: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    for unit_result in unit_results:
        unit_name = (
            str(unit_result.get("target_unit_name") or "").strip()
            or str(unit_result.get("unit_id") or "").strip()
        )
        if not unit_name:
            continue

        candidate_generation = unit_result.get("sql_candidate_generation", {})
        schema_linking = unit_result.get("schema_linking", {})
        question_generation = unit_result.get("question_generation", {})
        sql_candidates = []
        if isinstance(candidate_generation, dict):
            raw_candidates = candidate_generation.get("sql_candidates", [])
            if isinstance(raw_candidates, list):
                sql_candidates = [
                    str(candidate or "").strip()
                    for candidate in raw_candidates
                    if str(candidate or "").strip()
                ]
        payload = {
            "db_id": db_id,
            "unit_type": str(unit_result.get("target_unit_type") or ""),
            "question": str(unit_result.get("question") or ""),
            "sql_candidates": sql_candidates,
            "candidate_count": len(sql_candidates),
            "linked_tables": extract_fullnames(
                schema_linking.get("tables", []) if isinstance(schema_linking, dict) else []
            ),
            "linked_columns": extract_fullnames(
                schema_linking.get("columns", []) if isinstance(schema_linking, dict) else []
            ),
        }
        if isinstance(candidate_generation, dict):
            for key in ("engine_result_path", "wrapper_log_path"):
                value = str(candidate_generation.get(key) or "").strip()
                if value:
                    payload[key] = value
        error = ""
        if isinstance(question_generation, dict):
            error = str(question_generation.get("error") or "").strip()
        if not error and isinstance(candidate_generation, dict):
            error = str(candidate_generation.get("error") or "").strip()
        if error:
            payload["error"] = error
        output[unit_name] = payload
    return output


class ER2DataSQLCandidateRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        er2query_template_name: str = DEFAULT_ER2QUERY_TEMPLATE_NAME,
        question_model_config: str | None = None,
        candidate_model_config: str | None = None,
        reasoning_mode: str | None = None,
        include_desc_in_er2query: bool = True,
        include_conditions_in_er2query: bool = False,
        log_layout: str = "legacy",
        write_wrapper_logs: bool = False,
        create_schema_linking_log_dirs: bool = False,
    ) -> None:
        self.runner = ER2DataRunner(
            prompt_dir=prompt_dir,
            log_dir=log_dir,
            er2query_template_name=er2query_template_name,
            question_model_config=question_model_config,
            schema_link_model_config=candidate_model_config,
            nl2sql_model_config=candidate_model_config,
            reasoning_mode=reasoning_mode,
            include_desc_in_er2query=include_desc_in_er2query,
            include_conditions_in_er2query=include_conditions_in_er2query,
            include_conditions_in_sql2nl=False,
            log_layout=log_layout,
            write_wrapper_logs=write_wrapper_logs,
            create_schema_linking_log_dirs=create_schema_linking_log_dirs,
        )

    def run_case(
        self,
        *,
        input_path: str | Path,
        output_path: str | Path,
        db_id: str,
        engine_provider: str | None,
        spider2_root: str | Path | None,
        reforce_root: str | Path | None,
        engine_script: str | Path | None,
        nl2sql_engine_script: str | Path | None,
        max_question_concurrency: int | None,
        max_candidate_workers: int,
        candidate_timeout_seconds: float,
        candidate_temperature: float,
        schema_link_temperature: float,
        shortlist_trigger: int,
        max_shortlist_tables: int,
        sample_row_limit: int,
        sample_value_max_chars: int,
        similar_tables_hint_limit: int,
        num_votes: int,
        reforce_max_workers: int,
        max_iter: int,
        generation_model: str | None,
        column_exploration_model: str | None,
        vote_model: str | None,
        db_hint: str = "",
        enable_nl2sql_db_hint: bool = True,
        enable_er2query_db_hint: bool | None = None,
        enable_db_hint: bool | None = None,
        external_knowledge: str = "",
        include_er_diff: bool = True,
        er_diff_filename: str = DEFAULT_DIFF_INPUT_FILENAME,
    ) -> dict[str, Any]:
        runtime = resolve_engine_runtime(
            provider_name=engine_provider,
            spider2_root=spider2_root,
            reforce_root=reforce_root,
            engine_script=engine_script,
            nl2sql_engine_script=nl2sql_engine_script,
        )

        er_model = normalize_er_input_payload(
            read_json(input_path),
            input_path=input_path,
            merge_connections_into_relationships=False,
        )
        resolved_external_knowledge = str(external_knowledge or "").strip()
        if not resolved_external_knowledge:
            candidate = er_model.get("external_knowledge")
            if isinstance(candidate, str):
                resolved_external_knowledge = candidate.strip()
        if not resolved_external_knowledge:
            resolved_external_knowledge = read_external_knowledge_from_sidecar(input_path)

        if enable_db_hint is not None:
            enable_nl2sql_db_hint = bool(enable_db_hint)
        resolved_enable_er2query_db_hint = (
            enable_nl2sql_db_hint
            if enable_er2query_db_hint is None
            else bool(enable_er2query_db_hint)
        )
        prompt_context = resolve_er2query_prompt_context(
            er_model=er_model,
            input_path=input_path,
            db_hint=db_hint,
            enable_db_hint=resolved_enable_er2query_db_hint,
            external_knowledge=resolved_external_knowledge,
        )
        resolved_db_hint = resolve_db_hint(
            er_model=er_model,
            input_path=input_path,
            db_hint=db_hint,
            enable_db_hint=enable_nl2sql_db_hint,
        )

        primary_units = self.runner.build_units(er_model)
        diff_units: list[Any] = []
        resolved_diff_input_path: str | None = None
        if include_er_diff:
            diff_er_model, resolved_diff_input_path = load_optional_diff_er_model(
                input_path=input_path,
                diff_filename=er_diff_filename,
            )
            if diff_er_model is not None:
                diff_units = self.runner.build_units(diff_er_model)
        all_units = merge_primary_and_diff_units(primary_units, diff_units)
        step_started_at = time.perf_counter()
        unit_results = self.runner.generate_questions(
            all_units,
            prompt_context=prompt_context,
            max_concurrency=max_question_concurrency,
        )
        sql_candidate_target_unit_results = [
            item
            for item in unit_results
            if str(item.get("target_unit_type") or "").strip()
            in SQL_CANDIDATE_TARGET_UNIT_TYPES
        ]
        question_success_count = sum(
            1 for item in unit_results if item["question_generation"]["ok"]
        )
        sql_candidate_target_question_success_count = sum(
            1
            for item in sql_candidate_target_unit_results
            if item["question_generation"]["ok"]
        )
        skipped_unit_count = max(0, len(all_units) - len(sql_candidate_target_unit_results))
        emit_step_done_log(
            prefix="ER2DATA_2",
            step="generate_questions",
            elapsed_seconds=time.perf_counter() - step_started_at,
            units=len(all_units),
            questions=question_success_count,
            sql_candidate_target_questions=sql_candidate_target_question_success_count,
        )

        step_started_at = time.perf_counter()
        if sql_candidate_target_unit_results:
            self.run_sql_candidate_batch(
                sql_candidate_target_unit_results,
                db_id=db_id,
                base_external_knowledge=resolved_external_knowledge,
                base_db_hint=resolved_db_hint,
                max_workers=max_candidate_workers,
                provider_name=runtime.provider_name,
                runtime=runtime,
                timeout_seconds=candidate_timeout_seconds,
                candidate_temperature=candidate_temperature,
                schema_link_temperature=schema_link_temperature,
                shortlist_trigger=shortlist_trigger,
                max_shortlist_tables=max_shortlist_tables,
                sample_row_limit=sample_row_limit,
                sample_value_max_chars=sample_value_max_chars,
                similar_tables_hint_limit=similar_tables_hint_limit,
                num_votes=num_votes,
                reforce_max_workers=reforce_max_workers,
                max_iter=max_iter,
                generation_model=generation_model,
                column_exploration_model=column_exploration_model,
                vote_model=vote_model,
            )

        candidate_success_count = sum(
            1
            for item in unit_results
            if isinstance(item.get("sql_candidate_generation"), dict)
            and item["sql_candidate_generation"].get("ok")
        )
        emit_step_done_log(
            prefix="ER2DATA_2",
            step="run_sql_candidate_batch",
            elapsed_seconds=time.perf_counter() - step_started_at,
            units=len(sql_candidate_target_unit_results),
            workers=max(1, max_candidate_workers),
            candidate_ok=candidate_success_count,
        )

        ok = (
            question_success_count == len(all_units)
            and sql_candidate_target_question_success_count == len(sql_candidate_target_unit_results)
            and candidate_success_count == len(sql_candidate_target_unit_results)
        )
        metadata_payload = build_compact_sql_candidate_output(
            db_id=db_id,
            unit_results=sql_candidate_target_unit_results,
        )
        schema_linking_payload = build_schema_linking_output(
            db_id=db_id,
            unit_results=sql_candidate_target_unit_results,
        )
        output_path_resolved = resolve_path(output_path)
        schema_linking_output_path = output_path_resolved.with_name(
            DEFAULT_SCHEMA_LINKING_FILENAME
        )
        write_json(output_path, metadata_payload)
        write_json(schema_linking_output_path, schema_linking_payload)
        return {
            "ok": ok,
            "question_id": read_question_id(input_path),
            "db_id": db_id,
            "input_path": str(Path(input_path).resolve()),
            "diff_input_path": resolved_diff_input_path or "",
            "output_path": str(output_path_resolved),
            "schema_linking_output_path": str(schema_linking_output_path),
            "engine_provider": runtime.provider_name,
            "prompt_context": {
                "user_intent": str(prompt_context.get("user_intent") or ""),
                "db_hint": str(prompt_context.get("db_hint") or ""),
                "external_knowledge": str(prompt_context.get("external_knowledge") or ""),
            },
            "enable_nl2sql_db_hint": bool(enable_nl2sql_db_hint),
            "enable_er2query_db_hint": resolved_enable_er2query_db_hint,
            "summary": {
                "total_unit_count": len(all_units),
                "processed_unit_count": len(all_units),
                "primary_unit_count": len(primary_units),
                "diff_unit_count": len(diff_units),
                "sql_candidate_target_unit_count": len(sql_candidate_target_unit_results),
                "skipped_unit_count": skipped_unit_count,
                "question_success_count": question_success_count,
                "sql_candidate_target_question_success_count": (
                    sql_candidate_target_question_success_count
                ),
                "candidate_success_count": candidate_success_count,
            },
            "metadata_payload_keys": sorted(metadata_payload.keys()),
            "schema_linking_payload_keys": sorted(schema_linking_payload.keys()),
        }

    def run_sql_candidate_batch(
        self,
        unit_results: list[dict[str, Any]],
        *,
        db_id: str,
        base_external_knowledge: str,
        base_db_hint: str,
        max_workers: int,
        provider_name: str,
        runtime,
        timeout_seconds: float,
        candidate_temperature: float,
        schema_link_temperature: float,
        shortlist_trigger: int,
        max_shortlist_tables: int,
        sample_row_limit: int,
        sample_value_max_chars: int,
        similar_tables_hint_limit: int,
        num_votes: int,
        reforce_max_workers: int,
        max_iter: int,
        generation_model: str | None,
        column_exploration_model: str | None,
        vote_model: str | None,
    ) -> None:
        runnable_indices = [
            idx
            for idx, item in enumerate(unit_results)
            if isinstance(item.get("question"), str) and item["question"].strip()
        ]
        if not runnable_indices:
            return

        worker_count = max(1, min(max_workers, len(runnable_indices)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    self._run_single_sql_candidate,
                    unit_results[idx],
                    db_id=db_id,
                    base_external_knowledge=base_external_knowledge,
                    base_db_hint=base_db_hint,
                    provider_name=provider_name,
                    runtime=runtime,
                    timeout_seconds=timeout_seconds,
                    candidate_temperature=candidate_temperature,
                    schema_link_temperature=schema_link_temperature,
                    shortlist_trigger=shortlist_trigger,
                    max_shortlist_tables=max_shortlist_tables,
                    sample_row_limit=sample_row_limit,
                    sample_value_max_chars=sample_value_max_chars,
                    similar_tables_hint_limit=similar_tables_hint_limit,
                    num_votes=num_votes,
                    reforce_max_workers=reforce_max_workers,
                    max_iter=max_iter,
                    generation_model=generation_model,
                    column_exploration_model=column_exploration_model,
                    vote_model=vote_model,
                ): idx
                for idx in runnable_indices
            }

            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    summary_bundle = future.result()
                except Exception as exc:
                    summary_bundle = {
                        "nl2sql": {
                            "ok": False,
                            "skipped": False,
                            "engine_result_path": None,
                            "wrapper_log_path": None,
                            "result_sql": "",
                            "sql_candidates": [],
                            "error": f"SQL candidate execution failed: {exc}",
                            "stdout_excerpt": "",
                            "stderr_excerpt": "",
                        },
                        "schema_linking": {
                            "ok": False,
                            "skipped": False,
                            "engine_result_path": None,
                            "wrapper_log_path": None,
                            "tables": [],
                            "columns": [],
                            "error": f"SQL candidate execution failed: {exc}",
                            "stdout_excerpt": "",
                            "stderr_excerpt": "",
                        },
                        "sql_candidate_generation": {
                            "ok": False,
                            "skipped": False,
                            "engine_result_path": None,
                            "wrapper_log_path": None,
                            "sql_candidates": [],
                            "candidate_count": 0,
                            "error": f"SQL candidate execution failed: {exc}",
                            "stdout_excerpt": "",
                            "stderr_excerpt": "",
                        },
                    }
                unit_results[idx]["nl2sql"] = summary_bundle["nl2sql"]
                unit_results[idx]["schema_linking"] = summary_bundle["schema_linking"]
                unit_results[idx]["sql_candidate_generation"] = summary_bundle[
                    "sql_candidate_generation"
                ]

    def _build_candidate_external_knowledge(
        self,
        unit_result: dict[str, Any],
        *,
        base_external_knowledge: str,
        base_db_hint: str,
    ) -> str:
        unit_type = str(unit_result.get("target_unit_type") or "").strip()
        if unit_type == "relationship":
            return self.runner.build_relationship_external_knowledge(
                unit_result,
                base_external_knowledge=base_external_knowledge,
                base_db_hint=base_db_hint,
                entity_sql_by_name={},
            )
        return self.runner.build_entity_external_knowledge(
            unit_result,
            base_external_knowledge=base_external_knowledge,
            base_db_hint=base_db_hint,
        )

    def _run_single_sql_candidate(
        self,
        unit_result: dict[str, Any],
        *,
        db_id: str,
        base_external_knowledge: str,
        base_db_hint: str,
        provider_name: str,
        runtime,
        timeout_seconds: float,
        candidate_temperature: float,
        schema_link_temperature: float,
        shortlist_trigger: int,
        max_shortlist_tables: int,
        sample_row_limit: int,
        sample_value_max_chars: int,
        similar_tables_hint_limit: int,
        num_votes: int,
        reforce_max_workers: int,
        max_iter: int,
        generation_model: str | None,
        column_exploration_model: str | None,
        vote_model: str | None,
    ) -> dict[str, dict[str, Any]]:
        unit_id = str(unit_result["unit_id"])
        unit_file_stem = safe_file_stem(unit_id)
        engine_output_path = (self.runner.nl2sql_result_dir / f"{unit_file_stem}.json").resolve()
        wrapper_log_path = (self.runner.nl2sql_wrapper_dir / f"{unit_file_stem}.json").resolve()
        external_knowledge = self._build_candidate_external_knowledge(
            unit_result,
            base_external_knowledge=base_external_knowledge,
            base_db_hint=base_db_hint,
        )

        provider = get_engine_provider(provider_name)
        result = provider.run_nl2sql(
            NL2SQLRequest(
                question=str(unit_result["question"]),
                db_id=db_id,
                external_knowledge=external_knowledge,
                llm_config_name=self.runner.nl2sql_model_config_name,
                output_path=str(engine_output_path),
                temperature=candidate_temperature,
                schema_link_temperature=schema_link_temperature,
                shortlist_trigger=shortlist_trigger,
                max_shortlist_tables=max_shortlist_tables,
                sample_row_limit=sample_row_limit,
                sample_value_max_chars=sample_value_max_chars,
                similar_tables_hint_limit=similar_tables_hint_limit,
                num_votes=num_votes,
                max_workers=reforce_max_workers,
                max_iter=max_iter,
                timeout_seconds=timeout_seconds,
                generation_model=generation_model,
                column_exploration_model=column_exploration_model,
                vote_model=vote_model,
                return_candidates_only=True,
            ),
            runtime=runtime,
            raise_on_error=False,
        )

        resolved_wrapper_log_path = str(wrapper_log_path) if self.runner.write_wrapper_logs else None
        if resolved_wrapper_log_path:
            write_json(
                resolved_wrapper_log_path,
                sanitize_nl2sql_response(result.to_payload()),
            )

        stdout_excerpt = shorten_text(result.stdout)
        stderr_excerpt = shorten_text(result.stderr)
        sql_candidates = list(result.sql_candidates)
        candidate_ok = bool(sql_candidates)
        nl2sql_summary = {
            "ok": candidate_ok,
            "skipped": False,
            "engine_result_path": str(result.result_path or engine_output_path),
            "wrapper_log_path": resolved_wrapper_log_path,
            "result_sql": "",
            "sql_candidates": sql_candidates,
            "error": None if candidate_ok else result.error,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        }
        schema_linking_ok = bool(result.tables or result.columns)
        schema_linking_summary = {
            "ok": schema_linking_ok,
            "skipped": False,
            "engine_result_path": str(
                result.schema_linking_result_path or result.result_path or engine_output_path
            ),
            "wrapper_log_path": resolved_wrapper_log_path,
            "tables": result.tables,
            "columns": result.columns,
            "error": None if schema_linking_ok else result.error,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        }
        sql_candidate_summary = {
            "ok": candidate_ok,
            "skipped": False,
            "engine_result_path": str(result.result_path or engine_output_path),
            "wrapper_log_path": resolved_wrapper_log_path,
            "sql_candidates": sql_candidates,
            "candidate_count": len(sql_candidates),
            "error": None if candidate_ok else result.error,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        }
        return {
            "nl2sql": nl2sql_summary,
            "schema_linking": schema_linking_summary,
            "sql_candidate_generation": sql_candidate_summary,
        }


# Backwards-compatible import alias for callers that used the old schema-linking
# runner name before this script switched to SQL candidate generation.
ER2DataSchemaLinkRunner = ER2DataSQLCandidateRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read one metadata case directory or a metadata batch directory, generate "
            "ER2Query questions for all ER units, and generate SQL candidates for "
            "entity and relationship units."
        )
    )
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--question-model-config", default=None)
    parser.add_argument(
        "--candidate-model-config",
        "--nl2sql-model-config",
        dest="candidate_model_config",
        default=None,
        help="Model config for ER-unit NL2SQL candidate generation.",
    )
    parser.add_argument(
        "--schema-link-model-config",
        default=None,
        help="Deprecated alias for --candidate-model-config.",
    )
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument(
        "--enable-nl2sql-db-hint",
        "--enable-db-hint",
        dest="enable_nl2sql_db_hint",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
        help="Whether to pass `db_hint` into ER-unit NL2SQL candidate prompts.",
    )
    parser.add_argument(
        "--enable-er2query-db-hint",
        type=parse_cli_bool,
        default=None,
        metavar="{true,false}",
        help="Whether to pass `db_hint` into ER2Query prompts. Defaults to `--enable-nl2sql-db-hint` when omitted.",
    )
    parser.add_argument(
        "--er2query-template-name",
        default=DEFAULT_ER2QUERY_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--exclude-desc-in-er2query",
        type=parse_cli_bool,
        nargs="?",
        const=True,
        default=False,
        metavar="{true,false}",
        help="Do not pass target-unit desc into the ER2Query prompt.",
    )
    parser.add_argument(
        "--include-conditions-in-er2query",
        type=parse_cli_bool,
        nargs="?",
        const=True,
        default=False,
        metavar="{true,false}",
    )
    parser.add_argument("--max-batch-workers", type=positive_int, default=4)
    parser.add_argument("--max-question-concurrency", type=int, default=None)
    parser.add_argument(
        "--max-candidate-workers",
        "--max-nl2sql-workers",
        dest="max_candidate_workers",
        type=positive_int,
        default=None,
    )
    parser.add_argument(
        "--max-linking-workers",
        type=positive_int,
        default=4,
        help="Deprecated alias for --max-candidate-workers.",
    )
    parser.add_argument("--engine-provider", default="reforce_gen_sl_m1")
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument("--reforce-root", type=Path, default=DEFAULT_REFORCE_ROOT)
    parser.add_argument("--engine-script", type=Path, default=DEFAULT_ENGINE_SCRIPT)
    parser.add_argument("--nl2sql-engine-script", type=Path, default=None)
    parser.add_argument(
        "--candidate-timeout-seconds",
        "--nl2sql-timeout-seconds",
        dest="candidate_timeout_seconds",
        type=float,
        default=600.0,
    )
    parser.add_argument("--candidate-temperature", type=float, default=0.7)
    parser.add_argument("--schema-link-temperature", type=float, default=0.0)
    parser.add_argument("--shortlist-trigger", type=int, default=18)
    parser.add_argument("--max-shortlist-tables", type=int, default=24)
    parser.add_argument("--sample-row-limit", type=int, default=2)
    parser.add_argument("--sample-value-max-chars", type=int, default=300)
    parser.add_argument("--similar-tables-hint-limit", type=int, default=12)
    parser.add_argument("--num-votes", type=positive_int, default=4)
    parser.add_argument("--reforce-max-workers", type=positive_int, default=4)
    parser.add_argument("--max-iter", type=positive_int, default=5)
    parser.add_argument("--generation-model", default=None)
    parser.add_argument("--column-exploration-model", default=None)
    parser.add_argument("--vote-model", default=None)
    parser.add_argument(
        "--include-er-diff",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
        help="Whether to load optional ER diff file from the case directory.",
    )
    parser.add_argument(
        "--er-diff-filename",
        default=DEFAULT_DIFF_INPUT_FILENAME,
        help="Optional ER diff filename under each case directory.",
    )
    return parser.parse_args()


def run_single_case(
    *,
    case_input: CaseInput,
    batch_root: Path,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    case_dir = case_input.case_dir
    output_path = case_dir / DEFAULT_OUTPUT_FILENAME
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir

    sidecar_context = read_sidecar_context(case_input.input_path)
    question_id = (
        str(sidecar_context.get("question_id") or "").strip()
        or read_question_id(case_input.input_path)
        or case_dir.name
    )
    db_id = str(sidecar_context.get("db_id") or "").strip()
    if not db_id:
        raise ValueError(f"`db_id` is required for {case_input.input_path}")

    print(f"[ER2DATA_2] question_id={question_id}")
    print(f"[ER2DATA_2] case_dir={case_dir}")
    print(f"[ER2DATA_2] input_path={case_input.input_path}")
    print(f"[ER2DATA_2] output_path={output_path}")
    print(f"[ER2DATA_2] log_dir={log_dir}")

    resolved_candidate_model_config = (
        args.candidate_model_config or args.schema_link_model_config
    )
    resolved_max_candidate_workers = (
        args.max_candidate_workers or args.max_linking_workers
    )
    resolved_enable_er2query_db_hint = (
        args.enable_nl2sql_db_hint
        if args.enable_er2query_db_hint is None
        else args.enable_er2query_db_hint
    )

    runner = ER2DataSQLCandidateRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        er2query_template_name=args.er2query_template_name,
        question_model_config=args.question_model_config,
        candidate_model_config=resolved_candidate_model_config,
        reasoning_mode=args.reasoning_mode,
        include_desc_in_er2query=not args.exclude_desc_in_er2query,
        include_conditions_in_er2query=args.include_conditions_in_er2query,
    )
    result = runner.run_case(
        input_path=case_input.input_path,
        output_path=output_path,
        db_id=db_id,
        engine_provider=args.engine_provider,
        spider2_root=args.spider2_root,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
        nl2sql_engine_script=args.nl2sql_engine_script,
        max_question_concurrency=args.max_question_concurrency,
        max_candidate_workers=resolved_max_candidate_workers,
        candidate_timeout_seconds=args.candidate_timeout_seconds,
        candidate_temperature=args.candidate_temperature,
        schema_link_temperature=args.schema_link_temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.sample_row_limit,
        sample_value_max_chars=args.sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        num_votes=args.num_votes,
        reforce_max_workers=args.reforce_max_workers,
        max_iter=args.max_iter,
        generation_model=args.generation_model,
        column_exploration_model=args.column_exploration_model,
        vote_model=args.vote_model,
        db_hint=str(sidecar_context.get("db_hint") or "").strip(),
        enable_nl2sql_db_hint=args.enable_nl2sql_db_hint,
        enable_er2query_db_hint=resolved_enable_er2query_db_hint,
        external_knowledge=str(sidecar_context.get("external_knowledge") or "").strip(),
        include_er_diff=bool(args.include_er_diff),
        er_diff_filename=str(args.er_diff_filename),
    )
    summary = {
        "ok": bool(result.get("ok")),
        "question_id": question_id,
        "db_id": db_id,
        "case_dir": str(case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "input_path": str(case_input.input_path),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "engine_provider": args.engine_provider,
        "summary": result.get("summary", {}),
        "batch_root": str(batch_root),
    }
    write_json(log_dir / "case_summary.json", summary)
    return summary


def run_single_case_safe(
    *,
    case_input: CaseInput,
    batch_root: Path,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_path = case_input.case_dir / DEFAULT_OUTPUT_FILENAME
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    try:
        return run_single_case(
            case_input=case_input,
            batch_root=batch_root,
            run_timestamp=run_timestamp,
            args=args,
        )
    except Exception as exc:
        sidecar_context: dict[str, Any] = {}
        try:
            sidecar_context = read_sidecar_context(case_input.input_path)
        except Exception:
            sidecar_context = {}

        failure_payload = {
            "ok": False,
            "question_id": str(sidecar_context.get("question_id") or "").strip(),
            "db_id": str(sidecar_context.get("db_id") or "").strip(),
            "input_path": str(case_input.input_path),
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(output_path, failure_payload)
        write_json(log_dir / "error.json", failure_payload)
        return failure_payload


def main() -> None:
    args = parse_args()
    batch_root, case_inputs = discover_case_inputs(args.metadata_dir)
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()

    print(f"[ER2DATA_2] metadata_dir={resolve_path(args.metadata_dir)}")
    print(f"[ER2DATA_2] discovered_cases={len(case_inputs)}")
    print(f"[ER2DATA_2] run_timestamp={run_timestamp}")
    print(f"[ER2DATA_2] engine_provider={args.engine_provider}")
    print(f"[ER2DATA_2] max_batch_workers={args.max_batch_workers}")
    resolved_max_candidate_workers = args.max_candidate_workers or args.max_linking_workers
    print(f"[ER2DATA_2] max_candidate_workers={resolved_max_candidate_workers}")
    print(f"[ER2DATA_2] num_votes={args.num_votes}")
    print(f"[ER2DATA_2] reforce_max_workers={args.reforce_max_workers}")
    resolved_enable_er2query_db_hint = (
        args.enable_nl2sql_db_hint
        if args.enable_er2query_db_hint is None
        else args.enable_er2query_db_hint
    )
    print(f"[ER2DATA_2] enable_nl2sql_db_hint={args.enable_nl2sql_db_hint}")
    print(f"[ER2DATA_2] enable_er2query_db_hint={resolved_enable_er2query_db_hint}")
    print(f"[ER2DATA_2] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[ER2DATA_2] log_root={resolve_path(args.log_root)}")
    if args.reasoning_mode:
        print(f"[ER2DATA_2] reasoning_mode={args.reasoning_mode}")

    completed_summaries: list[dict[str, Any]] = []
    worker_count = max(1, min(args.max_batch_workers, len(case_inputs)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                run_single_case_safe,
                case_input=case_input,
                batch_root=batch_root,
                run_timestamp=run_timestamp,
                args=args,
            ): case_input
            for case_input in case_inputs
        }
        for future in as_completed(future_map):
            summary = future.result()
            completed_summaries.append(summary)
            print(
                "[ER2DATA_2] progress "
                f"completed={len(completed_summaries)}/{len(case_inputs)} "
                f"question_id={summary.get('question_id', '')} "
                f"ok={summary.get('ok', False)}"
            )

    success_count = sum(1 for item in completed_summaries if item.get("ok"))
    emit_step_done_log(
        prefix="ER2DATA_2",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
    )


if __name__ == "__main__":
    main()
