from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.er2data.schema_utils import read_json_object, resolve_path, table_fullname_from_column
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
from src.utils.run_log import write_json


@dataclass(slots=True)
class CaseInput:
    case_dir: Path
    relative_case_dir: Path
    schema_linking_path: Path
    output_path: Path | None = None


def build_target_table_prompt_snapshot(table_payload: dict[str, Any]) -> dict[str, Any]:
    columns = table_payload.get("columns")
    if columns is None:
        columns = table_payload.get("available_columns")
    sample_rows = table_payload.get("sample_rows")
    if sample_rows is None:
        sample_rows = table_payload.get("linked_sample_rows")
    return {
        "table_fullname": table_payload.get("table_fullname", ""),
        "table_name": table_payload.get("table_name", ""),
        "description": table_payload.get("description", ""),
        "columns": [
            {
                "column_fullname": column.get("column_fullname", ""),
                "column_name": column.get("column_name", ""),
            }
            for column in columns or []
            if isinstance(column, dict)
        ],
        "sample_rows": sample_rows or [],
    }


def collect_output_schema_selection(
    *,
    table_payloads: list[dict[str, Any]],
    sketches: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    linked_tables: list[str] = []
    linked_columns: list[str] = []
    seen_tables: set[str] = set()
    seen_columns: set[str] = set()

    available_by_table: dict[str, dict[str, str]] = {}
    for table_payload in table_payloads:
        table_fullname = str(table_payload.get("table_fullname") or "").strip()
        available_by_table[table_fullname] = {}
        target_table = table_payload.get("target_table") if isinstance(table_payload.get("target_table"), dict) else table_payload
        for column in target_table.get("available_columns", []) or target_table.get("columns", []) or []:
            if not isinstance(column, dict):
                continue
            column_name = str(column.get("column_name") or "").strip()
            column_fullname = str(column.get("column_fullname") or "").strip()
            if column_name and column_fullname:
                available_by_table[table_fullname][column_name.casefold()] = column_fullname

    def add_table(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_tables:
            return
        linked_tables.append(text)
        seen_tables.add(text)

    def add_column(value: Any, table_fullname: str = "") -> None:
        text = str(value or "").strip()
        if not text:
            return
        if not table_fullname_from_column(text) and table_fullname:
            text = available_by_table.get(table_fullname, {}).get(text.casefold(), "")
        if not text or text in seen_columns:
            return
        linked_columns.append(text)
        seen_columns.add(text)
        source_table = table_fullname_from_column(text)
        if source_table:
            add_table(source_table)

    for table_payload, sketch in zip(table_payloads, sketches):
        table_fullname = str(table_payload.get("table_fullname") or sketch.get("table_fullname") or "").strip()
        for column in unique_nonempty_strings(table_payload.get("linked_columns")):
            add_column(column, table_fullname)
        for column in unique_nonempty_strings(sketch.get("linked_columns")):
            add_column(column, table_fullname)
        for column in unique_nonempty_strings(sketch.get("other_columns")):
            add_column(column, table_fullname)
        for unit in sketch.get("semantic_units") or []:
            if isinstance(unit, dict):
                for column in collect_evidence_columns_from_unit(unit):
                    add_column(column, table_fullname)
    return linked_tables, linked_columns


class TableSemanticSketchRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        dry_run: bool = False,
    ) -> None:
        del model_config, reasoning_mode
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = resolve_path(log_dir)
        self.dry_run = dry_run
        self.table_prompt_dir = self.log_dir / "table_prompts"
        self.table_response_dir = self.log_dir / "table_responses"
        for directory in (self.log_dir, self.table_prompt_dir, self.table_response_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=False)

    def build_prompt(self, *, context: dict[str, Any], target_table: dict[str, Any]) -> str:
        return (
            "Task: table semantic sketch\n\n"
            f"## Original User Question\n{context.get('user_intent', '')}\n\n"
            f"## Resolve Process / Sub Questions\n{context.get('sub_questions', '')}\n\n"
            "## Analyze Target Table Schema\n"
            "```json\n"
            f"{json.dumps(target_table, ensure_ascii=False, indent=2)}\n"
            "```\n"
        )

    def _dry_run_profile(self, item: dict[str, Any]) -> dict[str, Any]:
        target_table = item["target_table"]
        table_fullname = str(target_table.get("table_fullname") or "").strip()
        attributes = [
            {
                "name": str(column.get("column_name") or "").strip(),
                "semantics": "Dry-run placeholder attribute support.",
                "evidence_columns": [str(column.get("column_fullname") or column.get("column_name") or "").strip()],
            }
            for column in target_table.get("columns", [])
            if isinstance(column, dict) and str(column.get("column_name") or "").strip()
        ]
        return {
            "table_fullname": table_fullname,
            "supported_question_semantics": [
                "Dry-run placeholder: inspect the generated prompt for intended table-level support."
            ],
            "is_relevant_supportive": bool(attributes),
            "is_relevant": bool(attributes),
            "semantic_units": [
                {
                    "unit_type": "entity",
                    "unit_name": str(target_table.get("table_name") or table_fullname).strip(),
                    "desc": "Dry-run placeholder semantic unit.",
                    "grain": "Dry-run placeholder grain.",
                    "attributes": attributes,
                    "participants": [],
                }
            ] if attributes else [],
            "prompt_path": item.get("prompt_path"),
            "response_path": item.get("response_path"),
        }

    def run_case(self, *, case_input: CaseInput, args: Any) -> dict[str, Any]:
        schema_linking_path = resolve_path(getattr(args, "schema_linking_path", None) or case_input.schema_linking_path)
        schema_linking_payload = read_json_object(schema_linking_path)
        question_payload = schema_linking_payload.get("question") if isinstance(schema_linking_payload.get("question"), dict) else {}
        context = {
            "question_id": question_payload.get("question_id", ""),
            "db_id": question_payload.get("db_id", getattr(args, "db_id", "") or ""),
            "user_intent": getattr(args, "question", None) or question_payload.get("question", ""),
            "db_hint": getattr(args, "db_hint", "") or "",
            "external_knowledge": getattr(args, "external_knowledge", "") or "",
            "sub_questions": read_sub_questions(
                case_dir=case_input.case_dir,
                nl2er_output_path=getattr(args, "nl2er_output_path", None),
                nl2er_output_filename=getattr(args, "nl2er_output_filename", DEFAULT_NL2ER_OUTPUT_FILENAME),
            ),
        }
        tables = []
        schema_snapshot = question_payload.get("schema_snapshot") if isinstance(question_payload, dict) else {}
        for table in schema_snapshot.get("tables", []) if isinstance(schema_snapshot, dict) else []:
            if isinstance(table, dict):
                tables.append(
                    {
                        "table_fullname": table.get("table_fullname", ""),
                        "target_table": sanitize_table_snapshot(
                            build_target_table_prompt_snapshot(table),
                            sample_row_limit=getattr(args, "sample_row_limit", DEFAULT_SAMPLE_ROW_LIMIT),
                            sample_values_per_column=1,
                            sample_value_max_chars=getattr(args, "sample_value_max_chars", DEFAULT_SAMPLE_VALUE_MAX_CHARS),
                        ),
                        "linked_columns": unique_nonempty_strings(
                            [column.get("column_fullname") for column in table.get("columns", []) if isinstance(column, dict)]
                        ),
                    }
                )

        items: list[dict[str, Any]] = []
        for index, table_payload in enumerate(tables):
            prompt = self.build_prompt(context=context, target_table=table_payload["target_table"])
            prompt_path = self.table_prompt_dir / f"table_{index + 1:04d}.md"
            response_path = self.table_response_dir / f"table_{index + 1:04d}.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            items.append({**table_payload, "prompt_path": str(prompt_path), "response_path": str(response_path)})

        profiles = [self._dry_run_profile(item) for item in items]
        linked_tables, linked_columns = collect_output_schema_selection(
            table_payloads=tables,
            sketches=profiles,
        )
        output_path = case_input.output_path or case_input.case_dir / "linked_table_profile.json"
        payload = {
            "ok": True,
            "question_id": context["question_id"],
            "db_id": context["db_id"],
            "question": context["user_intent"],
            "sub_questions": context["sub_questions"],
            "linked_tables": linked_tables,
            "linked_columns": linked_columns,
            "table_profiles": profiles,
        }
        write_json(output_path, payload)
        if bool(getattr(args, "write_enriched_schema_linking", False)):
            enriched = build_schema_linking_with_overall_result(
                schema_linking_payload=schema_linking_payload,
                context=context,
                linked_tables=linked_tables,
                linked_columns=linked_columns,
                source="table_semantic_sketch_output",
            )
            enriched["linked_table_profiles"] = profiles
            write_json(schema_linking_path, enriched)
        return payload
