from __future__ import annotations

import argparse
import time
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.er2data.physical_schema import load_database_tables
from src.er2data.schema_utils import DEFAULT_SAMPLE_VALUES_PER_COLUMN, read_sidecar_context, resolve_path, safe_file_stem
from src.er2data.table_grouping import (
    GROUPING_METHOD,
    TableGroup,
    build_table_groups,
    filter_groups_by_table_fullname,
    group_payloads_from_groups,
    normalize_casefold_set,
)
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.nl2er.concept_extraction import (
    OUTPUT_SOURCE,
    attach_table_group_contexts,
    build_compact_concept_output,
    normalize_concept_profile,
)
from src.nl2er.input_payloads import positive_int, read_inputs, select_inputs
from src.nl2er.schema_linking_semantic import (
    DEFAULT_PROMPT_DIR,
    DEFAULT_SAMPLE_ROW_LIMIT,
    DEFAULT_SAMPLE_VALUE_MAX_CHARS,
    DEFAULT_TABLE_SKETCH_CONCURRENCY,
    MAX_TABLE_SKETCH_CONCURRENCY,
    normalize_name_filters,
    resolve_table_sketch_concurrency,
)
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


DEFAULT_METADATA_DIR = Path("metadata")
DEFAULT_LOG_ROOT = Path("log/database_table_concept_extraction")
DEFAULT_OUTPUT_FILENAME = "database_table_concept_extraction.json"
TEMPLATE_KEY = "database_table_concept_extraction"
TEMPLATE_NAME = "Database_table_concept_extraction_v0.1.md"


@dataclass(slots=True)
class ConceptCaseInput:
    case_dir: Path
    relative_case_dir: Path
    output_path: Path | None
    context: dict[str, str] | None = None


@dataclass(slots=True)
class PreparedConceptCase:
    case_input: ConceptCaseInput
    runner: DatabaseTableConceptExtractionRunner
    context: dict[str, str]
    table_groups: list[TableGroup]
    group_payloads: list[dict[str, Any]]
    started_at: float


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract question-independent conceptual semantics from database "
            "physical table groups."
        )
    )
    parser.add_argument("--input-path", type=Path, default=None)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--question-id", nargs="+", default=None)
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--db-hint", default="")
    parser.add_argument(
        "--table-fullname",
        nargs="+",
        default=None,
        help="Optional table fullname filters. A group is kept when any member matches.",
    )
    parser.add_argument(
        "--table-context-scope",
        choices=("none", "schema", "database"),
        default="schema",
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
    parser.add_argument("--sample-value-max-chars", type=int, default=DEFAULT_SAMPLE_VALUE_MAX_CHARS)
    parser.add_argument("--max-workers", type=positive_int, default=None)
    parser.add_argument(
        "--max-table-concurrency",
        type=positive_int,
        default=None,
        help=(
            "Maximum parallel concept extraction LLM requests. Defaults to "
            f"{DEFAULT_TABLE_SKETCH_CONCURRENCY} and is capped at "
            f"{MAX_TABLE_SKETCH_CONCURRENCY}."
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def concept_case_output_path(
    *,
    case_input: ConceptCaseInput,
    log_dir: Path,
    output_filename: str,
) -> Path:
    return case_input.output_path or (log_dir / output_filename).resolve()


def context_from_input_payload(input_payload: Any) -> dict[str, str]:
    return {
        "question_id": str(getattr(input_payload, "question_id", "") or ""),
        "db_id": str(getattr(input_payload, "db_id", "") or ""),
        "db_hint": str(getattr(input_payload, "db_hint", "") or ""),
    }


def discover_concept_case_inputs(
    *,
    input_path: str | Path | None,
    metadata_dir: str | Path | None,
    output_path: str | Path | None,
    output_filename: str,
    requested_question_ids: set[str],
    db_id: str | None,
    db_hint: str,
) -> tuple[Path, list[ConceptCaseInput]]:
    if input_path is not None and metadata_dir is not None:
        raise ValueError("Use either --input-path or --metadata-dir, not both.")

    explicit_db_id = str(db_id or "").strip()
    if explicit_db_id:
        case_dir = resolve_path(metadata_dir or (Path(input_path).parent if input_path else Path.cwd()))
        return case_dir, [
            ConceptCaseInput(
                case_dir=case_dir,
                relative_case_dir=Path(safe_file_stem(explicit_db_id)),
                output_path=resolve_path(output_path) if output_path is not None else None,
                context={"db_id": explicit_db_id, "db_hint": str(db_hint or "").strip(), "question_id": ""},
            )
        ]

    if input_path is not None:
        resolved_input_path = resolve_path(input_path)
        selected_inputs = select_inputs(read_inputs(resolved_input_path), sorted(requested_question_ids))
        cases_by_db: dict[str, ConceptCaseInput] = {}
        for input_payload in selected_inputs:
            payload_db_id = str(input_payload.db_id or "").strip()
            if not payload_db_id or payload_db_id in cases_by_db:
                continue
            cases_by_db[payload_db_id] = ConceptCaseInput(
                case_dir=resolved_input_path.parent,
                relative_case_dir=Path(safe_file_stem(payload_db_id)),
                output_path=None,
                context=context_from_input_payload(input_payload),
            )
        cases = list(cases_by_db.values())
        if output_path is not None and len(cases) > 1:
            raise ValueError("--output-path can only be used when one database is selected.")
        if output_path is not None and cases:
            cases[0].output_path = resolve_path(output_path)
        return resolved_input_path, cases

    root_dir = resolve_path(metadata_dir or DEFAULT_METADATA_DIR)
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata directory does not exist: {root_dir}")
    if root_dir.is_file():
        raise ValueError(f"Expected metadata directory, got file: {root_dir}")

    direct_context = read_sidecar_context(root_dir)
    if direct_context.get("db_id"):
        return root_dir, [
            ConceptCaseInput(
                case_dir=root_dir,
                relative_case_dir=Path(safe_file_stem(direct_context["db_id"])),
                output_path=resolve_path(output_path) if output_path is not None else (root_dir / output_filename).resolve(),
                context=direct_context,
            )
        ]

    marker_paths = sorted(
        {
            path.resolve()
            for marker_name in ("input.json", "nl2er_input.json", "run_context.json")
            for path in root_dir.rglob(marker_name)
        }
    )
    cases: list[ConceptCaseInput] = []
    seen_db_ids: set[str] = set()
    for marker_path in marker_paths:
        case_dir = marker_path.parent
        context = read_sidecar_context(case_dir)
        context_db_id = str(context.get("db_id") or "").strip()
        if not context_db_id or context_db_id in seen_db_ids:
            continue
        cases.append(
            ConceptCaseInput(
                case_dir=case_dir,
                relative_case_dir=Path(safe_file_stem(context_db_id)),
                output_path=(case_dir / output_filename).resolve(),
                context=context,
            )
        )
        seen_db_ids.add(context_db_id)
    if not cases:
        raise FileNotFoundError(f"No db_id was found under {root_dir}.")
    return root_dir, cases


def normalize_context(
    *,
    case_input: ConceptCaseInput,
    args: argparse.Namespace,
) -> dict[str, str]:
    context = dict(case_input.context or read_sidecar_context(case_input.case_dir))
    if args.db_id is not None:
        context["db_id"] = str(args.db_id).strip()
    if args.db_hint:
        context["db_hint"] = str(args.db_hint).strip()
    context.setdefault("question_id", "")
    context.setdefault("db_hint", "")
    return context


class DatabaseTableConceptExtractionRunner:
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
                "db_id",
                "db_hint",
                "table_group_context",
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
        table_group_context = prompt_target_table.pop("table_group_context", {})
        return self.prompt_builder.build_text(
            TEMPLATE_KEY,
            vars={
                "db_id": context.get("db_id", ""),
                "db_hint": context.get("db_hint", ""),
                "table_group_context": table_group_context,
                "target_table_group": prompt_target_table,
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
            prompt = self.build_prompt(context=context, target_table=item["target_table"])
            stem = safe_file_stem(group_id or str(item.get("table_fullname") or "group"))
            prompt_path = self.group_prompt_dir / f"{stem}.md"
            response_path = self.group_response_dir / f"{stem}.json"
            prompt_path.write_text(prompt, encoding="utf-8")
            items.append({**item, "prompt_path": str(prompt_path), "response_path": str(response_path)})
            prompts.append(prompt)
        return items, prompts

    @staticmethod
    def build_dry_run_group_profile(item: dict[str, Any]) -> dict[str, Any]:
        target_table = item.get("target_table") if isinstance(item.get("target_table"), dict) else {}
        group = item.get("group") if isinstance(item.get("group"), dict) else {}
        representative_table = str(group.get("representative_table") or item.get("table_fullname") or "").strip()
        columns = [
            str(column.get("column_fullname") or column.get("column_name") or "").strip()
            for column in target_table.get("columns", [])
            if isinstance(column, dict) and str(column.get("column_name") or "").strip()
        ]
        return {
            "group_id": str(group.get("group_id") or item.get("group_id") or "").strip(),
            "grouping_method": GROUPING_METHOD,
            "representative_table": representative_table,
            "member_tables": group.get("member_tables") or [representative_table],
            "family_size": int(group.get("family_size") or 1),
            "member_table_scope": "all_members",
            "selected_member_tables": [],
            "concepts": [
                {
                    "concept_name": str(target_table.get("table_name") or representative_table).strip(),
                    "concept_type": "entity",
                    "description": "Dry-run placeholder concept inferred from the table group.",
                    "grain": "Dry-run placeholder grain.",
                    "identifier_columns": columns[:1],
                    "attribute_columns": [{"column": column, "data_role": "unknown"} for column in columns],
                    "participant_entities": [],
                    "evidence_columns": columns,
                }
            ] if columns else [],
            "foreign_key_candidates": [],
            "metadata_columns": [],
            "quality_warnings": ["Dry-run placeholder: no LLM concept extraction was performed."],
            "evidence_columns": columns,
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
            if not raw_response or not raw_response.strip():
                profile = self.build_dry_run_group_profile(item)
                profile["concepts"] = []
                profile["error"] = "LLM returned empty response."
                profiles.append(profile)
                continue
            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                profile = self.build_dry_run_group_profile(item)
                profile["concepts"] = []
                profile["error"] = f"Failed to parse database table concept extraction JSON: {exc}"
                profiles.append(profile)
                continue
            profiles.append(normalize_concept_profile(parsed=parsed, item=item))
        return profiles

    def prepare_case(
        self,
        *,
        case_input: ConceptCaseInput,
        args: argparse.Namespace,
    ) -> PreparedConceptCase:
        started_at = time.perf_counter()
        context = normalize_context(case_input=case_input, args=args)
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
        group_payloads = attach_table_group_contexts(
            table_groups=table_groups,
            group_payloads=group_payloads,
            scope=str(getattr(args, "table_context_scope", "schema") or "schema"),
        )
        return PreparedConceptCase(
            case_input=case_input,
            runner=self,
            context=context,
            table_groups=table_groups,
            group_payloads=group_payloads,
            started_at=started_at,
        )

    def analyze_groups(
        self,
        *,
        context: dict[str, str],
        group_payloads: list[dict[str, Any]],
        max_concurrency: int | None,
    ) -> list[dict[str, Any]]:
        items, prompts = self.prepare_group_prompts(context=context, group_payloads=group_payloads)
        if self.dry_run:
            return [self.build_dry_run_group_profile(item) for item in items]
        if self.llm is None:
            raise RuntimeError("LLM client is not initialized. Disable --dry-run to call the LLM.")
        raw_responses = self.llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(prompts) > 1,
            progress_desc="Database table concept extraction",
        )
        return self.parse_group_responses(items=items, raw_responses=raw_responses)

    def finalize_case(
        self,
        *,
        prepared: PreparedConceptCase,
        group_profiles: list[dict[str, Any]],
        args: argparse.Namespace,
    ) -> dict[str, Any]:
        compact_output = build_compact_concept_output(group_profiles)
        output_path = concept_case_output_path(
            case_input=prepared.case_input,
            log_dir=self.log_dir,
            output_filename=args.output_filename,
        )
        ok = all(not profile.get("error") for profile in group_profiles)
        output_payload = {
            "ok": ok,
            "method": OUTPUT_SOURCE,
            "grouping_method": GROUPING_METHOD,
            "db_id": prepared.context.get("db_id", ""),
            "db_hint": prepared.context.get("db_hint", ""),
            "output_path": str(output_path),
            "log_dir": str(self.log_dir),
            "raw_table_count": sum(len(group.members) for group in prepared.table_groups),
            "group_count": len(prepared.table_groups),
            "concept_count": len(compact_output["concepts"]),
            **compact_output,
            "table_concept_profiles": group_profiles,
            "elapsed_seconds": time.perf_counter() - prepared.started_at,
        }
        write_json(output_path, output_payload)
        write_json(self.log_dir / "output.json", output_payload)
        return {
            "ok": ok,
            "db_id": prepared.context.get("db_id", ""),
            "group_count": len(prepared.table_groups),
            "concept_count": len(compact_output["concepts"]),
            "output_path": str(output_path),
            "log_dir": str(self.log_dir),
        }

    def run_case(self, *, case_input: ConceptCaseInput, args: argparse.Namespace) -> dict[str, Any]:
        prepared = self.prepare_case(case_input=case_input, args=args)
        started_at = time.perf_counter()
        requested_concurrency = args.max_table_concurrency or args.max_workers
        group_profiles = self.analyze_groups(
            context=prepared.context,
            group_payloads=prepared.group_payloads,
            max_concurrency=resolve_table_sketch_concurrency(requested_concurrency),
        )
        emit_step_done_log(
            prefix="DATABASE_TABLE_CONCEPT_EXTRACTION",
            step="group_extraction",
            elapsed_seconds=time.perf_counter() - started_at,
            groups=len(group_profiles),
            dry_run=bool(self.dry_run),
        )
        return self.finalize_case(prepared=prepared, group_profiles=group_profiles, args=args)


def run_single_case(
    *,
    case_input: ConceptCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    runner = DatabaseTableConceptExtractionRunner(
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
    output_path = concept_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    return {
        "ok": bool(payload.get("ok")),
        "db_id": payload.get("db_id", ""),
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "group_count": int(payload.get("group_count") or 0),
        "concept_count": int(payload.get("concept_count") or 0),
        "dry_run": bool(args.dry_run),
    }


def run_single_case_safe(
    *,
    case_input: ConceptCaseInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    output_path = concept_case_output_path(
        case_input=case_input,
        log_dir=log_dir,
        output_filename=args.output_filename,
    )
    try:
        return run_single_case(case_input=case_input, run_timestamp=run_timestamp, args=args)
    except Exception as exc:
        log_dir.mkdir(parents=True, exist_ok=True)
        failure_payload = {
            "ok": False,
            "db_id": (case_input.context or {}).get("db_id", ""),
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


def main() -> None:
    args = parse_args()
    requested_question_ids = normalize_name_filters(args.question_id)
    batch_root, case_inputs = discover_concept_case_inputs(
        input_path=args.input_path,
        metadata_dir=args.metadata_dir,
        output_path=args.output_path,
        output_filename=args.output_filename,
        requested_question_ids=requested_question_ids,
        db_id=args.db_id,
        db_hint=args.db_hint,
    )
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()
    print(f"[DATABASE_TABLE_CONCEPT_EXTRACTION] batch_root={batch_root}")
    print(f"[DATABASE_TABLE_CONCEPT_EXTRACTION] discovered_cases={len(case_inputs)}")
    print(f"[DATABASE_TABLE_CONCEPT_EXTRACTION] run_timestamp={run_timestamp}")
    print(f"[DATABASE_TABLE_CONCEPT_EXTRACTION] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[DATABASE_TABLE_CONCEPT_EXTRACTION] log_root={resolve_path(args.log_root)}")
    print(f"[DATABASE_TABLE_CONCEPT_EXTRACTION] dry_run={bool(args.dry_run)}")

    completed_summaries = [
        run_single_case_safe(case_input=case_input, run_timestamp=run_timestamp, args=args)
        for case_input in case_inputs
    ]
    success_count = sum(1 for item in completed_summaries if item.get("ok"))
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
        prefix="DATABASE_TABLE_CONCEPT_EXTRACTION",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
        dry_run=bool(args.dry_run),
    )


if __name__ == "__main__":
    main()
