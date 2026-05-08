import argparse
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.nl2sql.base import SchemaLinkingRequest
from src.nl2sql.defaults import DEFAULT_REFORCE_ROOT, DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT
from src.nl2sql.registry import get_engine_provider, resolve_engine_runtime
from src.run.nl2er import DEFAULT_INPUT_PATH, DEFAULT_METADATA_ROOT, write_case_metadata
from src.run.nl2er_refine_0 import (
    DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    build_schema_evidence,
    ensure_dict_list,
    normalize_schema_linking_payload,
)
from src.run.nl2er_only import (
    BatchInput,
    build_case_summary,
    emit_batch_progress,
    normalize_question_ids,
    positive_int,
    read_inputs,
    select_inputs,
    serialize_input_payload,
)
from src.utils.run_log import build_timestamp, format_elapsed_seconds, resolve_run_dir, write_json
from src.validation.schema_linking_coverage import (
    format_batch_coverage_report,
    generate_batch_coverage_report,
)


DEFAULT_LOG_ROOT = Path("log/schema_linking")
DEFAULT_OUTPUT_FILENAME = "schema_linking.json"
DEFAULT_QUESTION_OUTPUT_FILENAME = "question_schema_linking.json"
SNAPSHOT_MODE_LINKED_COLUMNS = "linked_columns"
SNAPSHOT_MODE_LINKED_TABLE_ALL_COLUMNS = "linked_table_all_columns"


@dataclass(slots=True)
class MetadataQuestionCase:
    input_payload: BatchInput
    case_dir: Path
    relative_case_dir: Path
    source_input_path: Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run schema linking directly for one or more questions from the input file."
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument(
        "--metadata-dir",
        type=Path,
        default=None,
        help="Existing metadata case/batch directory. When set, questions are read from case input.json files.",
    )
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run. Supports multiple values, or a comma-separated list in one argument.",
    )
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    parser.add_argument("--schema-linking-log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument("--max-workers", type=positive_int, default=1)
    parser.add_argument("--engine-provider", default=None)
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument("--reforce-root", type=Path, default=DEFAULT_REFORCE_ROOT)
    parser.add_argument("--engine-script", type=Path, default=DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT)
    parser.add_argument("--python-executable", default=None)
    parser.add_argument("--schema-link-timeout-seconds", type=int, default=None)
    parser.add_argument("--schema-link-temperature", type=float, default=0.0)
    parser.add_argument("--shortlist-trigger", type=int, default=18)
    parser.add_argument("--max-shortlist-tables", type=int, default=24)
    parser.add_argument("--sample-row-limit", type=int, default=2)
    parser.add_argument("--sample-value-max-chars", type=int, default=300)
    parser.add_argument("--similar-tables-hint-limit", type=int, default=12)
    parser.add_argument(
        "--snapshot-mode",
        choices=[SNAPSHOT_MODE_LINKED_COLUMNS, SNAPSHOT_MODE_LINKED_TABLE_ALL_COLUMNS],
        default=SNAPSHOT_MODE_LINKED_COLUMNS,
        help="`linked_columns` stores only linked-column snapshots; `linked_table_all_columns` stores all columns from every linked table.",
    )
    parser.add_argument(
        "--snapshot-sample-values-per-column",
        type=positive_int,
        default=DEFAULT_SAMPLE_VALUES_PER_COLUMN,
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="Do not run whole-question schema linking; merge existing question/unit schema-linking files under --metadata-dir.",
    )
    parser.add_argument(
        "--merge-unit-schema-linking",
        action="store_true",
        help="Merge ER-unit schema linking with whole-question schema linking.",
    )
    parser.add_argument(
        "--question-schema-linking-filename",
        default=DEFAULT_QUESTION_OUTPUT_FILENAME,
    )
    parser.add_argument("--unit-schema-linking-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--merged-output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument(
        "--unit-schema-linking-backup-filename",
        default=None,
        help="Deprecated; merge now reads and overwrites the target schema_linking.json in place.",
    )
    parser.add_argument(
        "--ground-truth-dir",
        type=Path,
        default=None,
        help="When provided, run schema-linking coverage after the batch finishes.",
    )
    parser.add_argument("--coverage-output-path", type=Path, default=None)
    parser.add_argument(
        "--coverage-schema-linking-filename",
        default=None,
        help="Schema-linking filename to use for coverage. Defaults to --output-filename.",
    )
    parser.add_argument("--coverage-dialect", default=None)
    parser.add_argument("--show-covered-details", action="store_true")
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Reduce console output; full details are still written to metadata/log files.",
    )
    return parser.parse_args()


def extract_fullnames(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []

    fullnames: list[str] = []
    seen: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            fullname = str(item.get("fullname") or "").strip()
        else:
            fullname = str(item or "").strip()
        if not fullname or fullname in seen:
            continue
        fullnames.append(fullname)
        seen.add(fullname)
    return fullnames


def read_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object at {file_path}, got {type(payload).__name__}."
        )
    return payload


def read_optional_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    return read_json_object(file_path)


def discover_metadata_question_cases(metadata_dir: str | Path) -> tuple[Path, list[MetadataQuestionCase]]:
    root_dir = Path(metadata_dir).expanduser()
    if not root_dir.is_absolute():
        root_dir = (Path.cwd() / root_dir).resolve()
    else:
        root_dir = root_dir.resolve()
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata directory does not exist: {root_dir}")
    if root_dir.is_file():
        raise ValueError(f"Expected a metadata directory, got file: {root_dir}")

    input_paths: list[Path]
    direct_input = root_dir / "input.json"
    if direct_input.exists():
        input_paths = [direct_input.resolve()]
    else:
        input_paths = sorted({path.resolve() for path in root_dir.rglob("input.json")})
    if not input_paths:
        raise FileNotFoundError(f"No `input.json` files were found under {root_dir}")

    cases: list[MetadataQuestionCase] = []
    seen_question_ids: set[str] = set()
    for input_path in input_paths:
        parsed_inputs = read_inputs(input_path)
        if len(parsed_inputs) != 1:
            raise ValueError(f"Expected one input object at {input_path}, got {len(parsed_inputs)}.")
        input_payload = parsed_inputs[0]
        if input_payload.question_id in seen_question_ids:
            raise ValueError(
                f"Duplicate question_id `{input_payload.question_id}` found under {root_dir}"
            )
        seen_question_ids.add(input_payload.question_id)
        case_dir = input_path.parent
        cases.append(
            MetadataQuestionCase(
                input_payload=input_payload,
                case_dir=case_dir,
                relative_case_dir=case_dir.relative_to(root_dir),
                source_input_path=input_path,
            )
        )
    return root_dir, cases


def build_schema_linking_unit(
    *,
    input_payload: BatchInput,
    engine_result_payload: dict[str, Any],
    schema_snapshot_payload: dict[str, Any] | None = None,
    snapshot_mode: str = SNAPSHOT_MODE_LINKED_COLUMNS,
) -> dict[str, Any]:
    linked_tables = extract_fullnames(engine_result_payload.get("tables", []))
    linked_columns = extract_fullnames(engine_result_payload.get("columns", []))
    ok = bool(engine_result_payload.get("ok"))

    unit_payload: dict[str, Any] = {
        "unit_name": "question",
        "unit_type": "question",
        "question_id": input_payload.question_id,
        "db_id": input_payload.db_id,
        "question": input_payload.user_intent,
        "linked_tables": linked_tables,
        "linked_columns": linked_columns,
        "snapshot_mode": snapshot_mode,
        "schema_snapshot": schema_snapshot_payload or {},
        "schema_linking": {
            "ok": ok,
            "engine": engine_result_payload.get("engine"),
            "engine_result_path": engine_result_payload.get("result_path"),
            "table_count": len(linked_tables),
            "column_count": len(linked_columns),
        },
    }
    error = str(engine_result_payload.get("error") or "").strip()
    if error:
        unit_payload["error"] = error
        unit_payload["schema_linking"]["error"] = error
    warning = str(engine_result_payload.get("warning") or "").strip()
    if warning:
        unit_payload["warning"] = warning
        unit_payload["schema_linking"]["warning"] = warning
    return unit_payload


def build_output_payload(
    *,
    input_payload: BatchInput,
    engine_result_payload: dict[str, Any],
    schema_snapshot_payload: dict[str, Any],
    snapshot_mode: str,
) -> dict[str, Any]:
    return {
        "question": build_schema_linking_unit(
            input_payload=input_payload,
            engine_result_payload=engine_result_payload,
            schema_snapshot_payload=schema_snapshot_payload,
            snapshot_mode=snapshot_mode,
        )
    }


def _project_sample_rows(
    sample_rows: Any,
    columns: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(sample_rows, list):
        return []
    column_names = [
        str(column.get("column_name") or "").strip()
        for column in columns
        if str(column.get("column_name") or "").strip()
    ]
    if not column_names:
        return []

    projected_rows: list[dict[str, Any]] = []
    for row in sample_rows:
        if not isinstance(row, dict):
            continue
        projected = {
            column_name: row.get(column_name)
            for column_name in column_names
            if column_name in row
        }
        if projected:
            projected_rows.append(projected)
    return projected_rows


def project_schema_snapshot(
    *,
    evidence_payload: dict[str, Any],
    snapshot_mode: str,
) -> dict[str, Any]:
    projected_tables: list[dict[str, Any]] = []
    selected_column_count = 0
    for table in ensure_dict_list(evidence_payload.get("tables")):
        if snapshot_mode == SNAPSHOT_MODE_LINKED_TABLE_ALL_COLUMNS:
            selected_columns = ensure_dict_list(table.get("available_columns"))
            sample_rows = list(table.get("sample_rows") or [])
        else:
            selected_columns = ensure_dict_list(table.get("linked_columns"))
            sample_rows = list(table.get("linked_sample_rows") or [])
            if not sample_rows:
                sample_rows = _project_sample_rows(table.get("sample_rows"), selected_columns)

        clean_columns = [
            {key: value for key, value in column.items() if key != "is_linked_column"}
            for column in selected_columns
        ]
        selected_column_count += len(clean_columns)

        table_payload: dict[str, Any] = {
            "table_fullname": str(table.get("table_fullname") or "").strip(),
            "table_name": str(table.get("table_name") or "").strip(),
            "description": str(table.get("description") or "").strip(),
            "snapshot_path": str(table.get("snapshot_path") or "").strip(),
            "columns": clean_columns,
            "sample_rows": sample_rows,
        }
        warning = str(table.get("warning") or "").strip()
        if warning:
            table_payload["warning"] = warning
        projected_tables.append(table_payload)

    return {
        "db_id": str(evidence_payload.get("db_id") or "").strip(),
        "snapshot_mode": snapshot_mode,
        "evidence_scope": "question_linked_tables",
        "column_scope": (
            "all_columns_from_linked_tables"
            if snapshot_mode == SNAPSHOT_MODE_LINKED_TABLE_ALL_COLUMNS
            else "linked_columns_only"
        ),
        "snapshot_search_roots": list(evidence_payload.get("snapshot_search_roots") or []),
        "table_count": len(projected_tables),
        "column_count": selected_column_count,
        "tables": projected_tables,
        "unresolved_linked_columns": list(
            evidence_payload.get("unresolved_linked_columns") or []
        ),
    }


def build_question_schema_snapshot(
    *,
    input_payload: BatchInput,
    engine_result_payload: dict[str, Any],
    runtime_spider2_root: Path | None,
    snapshot_mode: str,
    sample_values_per_column: int,
) -> dict[str, Any]:
    schema_linking_payload = build_schema_linking_unit(
        input_payload=input_payload,
        engine_result_payload=engine_result_payload,
        schema_snapshot_payload={},
        snapshot_mode=snapshot_mode,
    )
    evidence_payload = build_schema_evidence(
        db_id=input_payload.db_id,
        schema_linking_by_unit=normalize_schema_linking_payload(
            {"question": schema_linking_payload}
        ),
        spider2_root=runtime_spider2_root,
        sample_values_per_column=sample_values_per_column,
    )
    return project_schema_snapshot(
        evidence_payload=evidence_payload,
        snapshot_mode=snapshot_mode,
    )


def collect_unique_schema_names(*payloads: dict[str, Any]) -> tuple[list[str], list[str]]:
    table_set: set[str] = set()
    column_set: set[str] = set()
    for payload in payloads:
        if not payload:
            continue
        normalized = normalize_schema_linking_payload(payload)
        for unit_payload in normalized.values():
            if not isinstance(unit_payload, dict):
                continue
            table_set.update(extract_fullnames(unit_payload.get("linked_tables")))
            column_set.update(extract_fullnames(unit_payload.get("linked_columns")))

    for column_fullname in list(column_set):
        parts = [part for part in str(column_fullname or "").split(".") if part]
        if len(parts) > 1:
            table_set.add(".".join(parts[:-1]))

    return sorted(table_set), sorted(column_set)


def build_merged_schema_linking_payload(
    *,
    input_payload: BatchInput,
    question_schema_linking_payload: dict[str, Any],
    unit_schema_linking_payload: dict[str, Any],
    snapshot_mode: str,
    sample_values_per_column: int,
    spider2_root: Path | None,
) -> dict[str, Any]:
    linked_tables, linked_columns = collect_unique_schema_names(
        question_schema_linking_payload,
        unit_schema_linking_payload,
    )
    schema_linking_by_unit: dict[str, Any] = {}
    if question_schema_linking_payload:
        schema_linking_by_unit["__whole_question__"] = {
            "db_id": input_payload.db_id,
            "question": input_payload.user_intent,
            "linked_tables": linked_tables,
            "linked_columns": linked_columns,
        }
    if unit_schema_linking_payload:
        schema_linking_by_unit.update(normalize_schema_linking_payload(unit_schema_linking_payload))

    evidence_payload = build_schema_evidence(
        db_id=input_payload.db_id,
        schema_linking_by_unit=schema_linking_by_unit,
        spider2_root=spider2_root,
        sample_values_per_column=sample_values_per_column,
    )
    schema_snapshot_payload = project_schema_snapshot(
        evidence_payload=evidence_payload,
        snapshot_mode=snapshot_mode,
    )

    payload: dict[str, Any] = {
        "question": {
            "unit_name": "question",
            "unit_type": "question",
            "question_id": input_payload.question_id,
            "db_id": input_payload.db_id,
            "question": input_payload.user_intent,
            "linked_tables": linked_tables,
            "linked_columns": linked_columns,
            "snapshot_mode": snapshot_mode,
            "schema_snapshot": schema_snapshot_payload,
            "schema_linking": {
                "ok": bool(linked_tables or linked_columns),
                "source": "merged_question_and_er_units",
                "table_count": len(linked_tables),
                "column_count": len(linked_columns),
            },
        },
        "sources": {
            "whole_question": question_schema_linking_payload,
            "er_units": unit_schema_linking_payload,
        },
    }
    if unit_schema_linking_payload:
        payload["units"] = unit_schema_linking_payload
    return payload


def write_case_failure_log(
    *,
    log_dir: Path,
    input_payload: BatchInput,
    run_id: str,
    error_type: str,
    error_message: str,
    traceback_text: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "question_id": input_payload.question_id,
        "db_id": input_payload.db_id,
        "error_type": error_type,
        "error_message": error_message,
    }
    if traceback_text:
        payload["traceback"] = traceback_text
    write_json(log_dir / "error.json", payload)


def build_unhandled_schema_linking_case_summary(
    *,
    input_payload: BatchInput,
    run_timestamp: str,
    args: argparse.Namespace,
    error_message: str,
) -> dict[str, object]:
    metadata_dir = (
        Path(args.metadata_root).expanduser().resolve()
        / run_timestamp
        / f"{input_payload.question_id}_{run_timestamp}"
    )
    log_dir = (
        Path(args.schema_linking_log_root).expanduser().resolve()
        / run_timestamp
        / f"{input_payload.question_id}_{run_timestamp}"
    )
    summary = build_case_summary(
        input_payload=input_payload,
        run_id=f"{input_payload.question_id}_{run_timestamp}",
        run_timestamp=run_timestamp,
        metadata_dir=metadata_dir,
        log_dir=log_dir,
        output_path=metadata_dir / DEFAULT_OUTPUT_FILENAME,
        mode="schema_linking_only",
        status="failed",
        error_message=error_message,
    )
    summary["snapshot_mode"] = args.snapshot_mode
    return summary


def run_single_question(
    *,
    input_payload: BatchInput,
    run_timestamp: str,
    args: argparse.Namespace,
    metadata_dir_override: Path | None = None,
    log_relative_dir: Path | None = None,
    source_input_path: Path | None = None,
) -> dict[str, object]:
    started_at = time.perf_counter()
    run_id = f"{input_payload.question_id}_{run_timestamp}"
    if metadata_dir_override is None:
        metadata_dir = resolve_run_dir(
            run_prefix=input_payload.question_id,
            run_dir=None,
            run_root=args.metadata_root / run_timestamp,
            timestamp=run_timestamp,
        )
        log_dir = resolve_run_dir(
            run_prefix=input_payload.question_id,
            run_dir=None,
            run_root=args.schema_linking_log_root / run_timestamp,
            timestamp=run_timestamp,
        )
        resolved_source_input_path = args.input_path
    else:
        metadata_dir = metadata_dir_override
        metadata_dir.mkdir(parents=True, exist_ok=True)
        relative_dir = log_relative_dir or Path(metadata_dir.name)
        log_dir = Path(args.schema_linking_log_root).expanduser()
        if not log_dir.is_absolute():
            log_dir = (Path.cwd() / log_dir).resolve()
        log_dir = log_dir / run_timestamp / relative_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        resolved_source_input_path = source_input_path or (metadata_dir / "input.json")
    output_path = metadata_dir / args.output_filename
    engine_output_path = log_dir / "schema_linking_engine_result.json"
    existing_unit_schema_linking_payload: dict[str, Any] = {}
    if args.merge_unit_schema_linking and metadata_dir_override is not None:
        existing_unit_schema_linking_payload = read_optional_json_object(
            metadata_dir / args.unit_schema_linking_filename
        )

    serialized_input = serialize_input_payload(input_payload)
    write_case_metadata(
        metadata_dir=metadata_dir,
        serialized_input=serialized_input,
        source_input_path=resolved_source_input_path,
        question_id=input_payload.question_id,
    )
    write_json(log_dir / "input.json", serialized_input)
    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": input_payload.question_id,
            "db_id": input_payload.db_id,
            "timestamp": run_timestamp,
            "mode": "schema_linking_only",
            "snapshot_mode": args.snapshot_mode,
            "input_path": str(resolved_source_input_path),
            "metadata_dir": str(metadata_dir),
            "log_dir": str(log_dir),
            "output_path": str(output_path),
            "model_config": args.model_config,
            "reasoning_mode": args.reasoning_mode,
            "engine_provider": args.engine_provider,
        },
    )

    if not args.quiet:
        print(f"[SCHEMA-LINKING-ONLY] run_id={run_id}")
        print(f"[SCHEMA-LINKING-ONLY] question_id={input_payload.question_id}")
        print(f"[SCHEMA-LINKING-ONLY] db_id={input_payload.db_id}")
        print(f"[SCHEMA-LINKING-ONLY] metadata_dir={metadata_dir}")
        print(f"[SCHEMA-LINKING-ONLY] log_dir={log_dir}")

    try:
        settings = load_settings()
        llm_config = apply_reasoning_mode(
            settings.llm.get(args.model_config),
            args.reasoning_mode,
        )
        provider = get_engine_provider(args.engine_provider)
        runtime = resolve_engine_runtime(
            provider_name=args.engine_provider,
            spider2_root=args.spider2_root,
            reforce_root=args.reforce_root,
            engine_script=args.engine_script,
            python_executable=args.python_executable,
        )
        result = provider.run_schema_linking(
            SchemaLinkingRequest(
                api_key=llm_config.api_key,
                base_url=llm_config.base_url,
                db_id=input_payload.db_id,
                question=input_payload.user_intent,
                run_prefix=run_id,
                model=llm_config.model,
                thinking_type=llm_config.thinking_type,
                reasoning_effort=llm_config.reasoning_effort,
                output_path=str(engine_output_path),
                temperature=args.schema_link_temperature,
                shortlist_trigger=args.shortlist_trigger,
                max_shortlist_tables=args.max_shortlist_tables,
                sample_row_limit=args.sample_row_limit,
                sample_value_max_chars=args.sample_value_max_chars,
                similar_tables_hint_limit=args.similar_tables_hint_limit,
                timeout_seconds=args.schema_link_timeout_seconds,
            ),
            runtime=runtime,
            raise_on_error=False,
        )
        result_payload = result.to_payload()
        schema_snapshot_payload = build_question_schema_snapshot(
            input_payload=input_payload,
            engine_result_payload=result_payload,
            runtime_spider2_root=runtime.spider2_root,
            snapshot_mode=args.snapshot_mode,
            sample_values_per_column=args.snapshot_sample_values_per_column,
        )
        write_json(log_dir / "schema_linking_result_payload.json", result_payload)
        write_json(log_dir / "schema_snapshot.json", schema_snapshot_payload)
        question_schema_linking_payload = build_output_payload(
            input_payload=input_payload,
            engine_result_payload=result_payload,
            schema_snapshot_payload=schema_snapshot_payload,
            snapshot_mode=args.snapshot_mode,
        )
        if args.merge_unit_schema_linking and existing_unit_schema_linking_payload:
            output_payload = build_merged_schema_linking_payload(
                input_payload=input_payload,
                question_schema_linking_payload=question_schema_linking_payload,
                unit_schema_linking_payload=existing_unit_schema_linking_payload,
                snapshot_mode=args.snapshot_mode,
                sample_values_per_column=args.snapshot_sample_values_per_column,
                spider2_root=runtime.spider2_root,
            )
        else:
            output_payload = question_schema_linking_payload
        write_json(output_path, output_payload)
    except Exception as exc:
        error_message = str(exc)
        run_summary = build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            log_dir=log_dir,
            output_path=output_path,
            mode="schema_linking_only",
            status="failed",
            elapsed_seconds=time.perf_counter() - started_at,
            error_message=error_message,
        )
        run_summary["snapshot_mode"] = args.snapshot_mode
        write_case_failure_log(
            log_dir=log_dir,
            input_payload=input_payload,
            run_id=run_id,
            error_type=type(exc).__name__,
            error_message=error_message,
            traceback_text=traceback.format_exc(),
        )
        write_json(metadata_dir / "run_context.json", run_summary)
        write_json(
            output_path,
            {
                "question": {
                    "unit_name": "question",
                    "unit_type": "question",
                    "question_id": input_payload.question_id,
                    "db_id": input_payload.db_id,
                    "question": input_payload.user_intent,
                    "linked_tables": [],
                    "linked_columns": [],
                    "snapshot_mode": args.snapshot_mode,
                    "schema_snapshot": {},
                    "error": error_message,
                }
            },
        )
        if not args.quiet:
            print(f"[SCHEMA-LINKING-ONLY] run failed for {input_payload.question_id}: {exc}")
        return run_summary

    status = "succeeded" if result_payload.get("ok") else "failed"
    error_message = None if result_payload.get("ok") else str(result_payload.get("error") or "")
    run_summary = build_case_summary(
        input_payload=input_payload,
        run_id=run_id,
        run_timestamp=run_timestamp,
        metadata_dir=metadata_dir,
        log_dir=log_dir,
        output_path=output_path,
        mode="schema_linking_only",
        status=status,
        elapsed_seconds=time.perf_counter() - started_at,
        error_message=error_message,
    )
    run_summary["snapshot_mode"] = args.snapshot_mode
    write_json(metadata_dir / "run_context.json", run_summary)

    if args.quiet:
        return run_summary

    if result_payload.get("ok"):
        print(
            f"[SCHEMA-LINKING-ONLY] output wrote to {output_path} "
            f"tables={len(result_payload.get('tables') or [])} "
            f"columns={len(result_payload.get('columns') or [])} "
            f"elapsed={format_elapsed_seconds(run_summary['elapsed_seconds'])}"
        )
    else:
        print(
            f"[SCHEMA-LINKING-ONLY] schema linking failed for {input_payload.question_id}: "
            f"{error_message}"
        )
    return run_summary


def run_merge_metadata_case(
    *,
    case: MetadataQuestionCase,
    args: argparse.Namespace,
) -> dict[str, object]:
    question_path = case.case_dir / args.question_schema_linking_filename
    unit_primary_path = case.case_dir / args.unit_schema_linking_filename
    unit_path = unit_primary_path
    output_path = case.case_dir / args.merged_output_filename

    question_payload = read_optional_json_object(question_path)
    unit_payload = read_optional_json_object(unit_path)

    if not question_payload and not unit_payload:
        raise FileNotFoundError(
            "No schema-linking input was found for merge: "
            f"{question_path} or {unit_path}"
        )

    merged_payload = build_merged_schema_linking_payload(
        input_payload=case.input_payload,
        question_schema_linking_payload=question_payload,
        unit_schema_linking_payload=unit_payload if args.merge_unit_schema_linking else {},
        snapshot_mode=args.snapshot_mode,
        sample_values_per_column=args.snapshot_sample_values_per_column,
        spider2_root=args.spider2_root,
    )
    write_json(output_path, merged_payload)
    return {
        "question_id": case.input_payload.question_id,
        "db_id": case.input_payload.db_id,
        "status": "succeeded",
        "case_dir": str(case.case_dir),
        "question_schema_linking_path": str(question_path),
        "unit_schema_linking_path": str(unit_path) if unit_payload else "",
        "output_path": str(output_path),
        "linked_table_count": len(merged_payload["question"].get("linked_tables") or []),
        "linked_column_count": len(merged_payload["question"].get("linked_columns") or []),
    }


def run_merge_metadata_cases(
    *,
    cases: list[MetadataQuestionCase],
    args: argparse.Namespace,
) -> list[dict[str, object]]:
    summaries: list[dict[str, object]] = []
    for case in cases:
        try:
            summary = run_merge_metadata_case(case=case, args=args)
        except Exception as exc:
            summary = {
                "question_id": case.input_payload.question_id,
                "db_id": case.input_payload.db_id,
                "status": "failed",
                "case_dir": str(case.case_dir),
                "error_message": str(exc),
            }
            if not args.quiet:
                print(
                    f"[SCHEMA-LINKING-ONLY] merge failed for "
                    f"{case.input_payload.question_id}: {exc}"
                )
        summaries.append(summary)
    return summaries


def run_coverage_check(
    *,
    batch_metadata_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    if args.ground_truth_dir is None:
        return {}

    report = generate_batch_coverage_report(
        metadata_dir=batch_metadata_dir,
        ground_truth_dir=args.ground_truth_dir,
        dialect=args.coverage_dialect,
        schema_linking_filename=args.coverage_schema_linking_filename or args.output_filename,
    )
    rendered_text = format_batch_coverage_report(
        report,
        show_covered_details=args.show_covered_details,
    )
    output_path = args.coverage_output_path or (
        batch_metadata_dir / "schema_linking_coverage_report.txt"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rendered_text, encoding="utf-8")
    if args.quiet:
        print(format_quiet_coverage_report(report, report_path=output_path))
    else:
        print(rendered_text)
        print(f"[SCHEMA-LINKING-ONLY] coverage report wrote to {output_path}")
    return report.to_payload()


def format_quiet_coverage_report(report: Any, *, report_path: Path) -> str:
    lines = [
        (
            "[SCHEMA-LINKING-ONLY] coverage "
            f"total={report.total_cases} "
            f"ok={report.ok_cases} "
            f"covered={report.covered_cases} "
            f"uncovered={report.uncovered_cases} "
            f"errors={report.error_cases} "
            f"strict_table_recall_rate={report.strict_table_recall_rate:.3f} "
            f"strict_column_recall_rate={report.strict_column_recall_rate:.3f}"
        )
    ]

    for item in report.cases:
        if not item.ok or item.coverage is None:
            lines.append(
                f"[SCHEMA-LINKING-ONLY] {item.question_id} ERROR {item.error or ''}".rstrip()
            )
            continue

        coverage = item.coverage
        lines.append(
            f"[SCHEMA-LINKING-ONLY] {item.question_id} "
            f"table_precision={coverage.table_coverage.precision:.3f} "
            f"table_recall={coverage.table_coverage.recall:.3f} "
            f"column_precision={coverage.resolved_column_coverage.precision:.3f} "
            f"column_recall={coverage.resolved_column_coverage.recall:.3f}"
        )
        lines.append(
            "[SCHEMA-LINKING-ONLY] "
            f"{item.question_id} missing_tables="
            + (", ".join(coverage.table_coverage.missing) or "-")
        )
        lines.append(
            "[SCHEMA-LINKING-ONLY] "
            f"{item.question_id} missing_columns="
            + (", ".join(coverage.resolved_column_coverage.missing) or "-")
        )

    lines.append(f"[SCHEMA-LINKING-ONLY] coverage_report={report_path}")
    return "\n".join(lines)


def main() -> None:
    args = parse_args()

    batch_timestamp = build_timestamp()
    requested_question_ids = normalize_question_ids(args.question_id)

    metadata_cases: list[MetadataQuestionCase] = []
    selected_inputs: list[BatchInput] = []
    if args.metadata_dir is not None:
        batch_metadata_dir, discovered_cases = discover_metadata_question_cases(args.metadata_dir)
        if requested_question_ids:
            case_by_question_id = {case.input_payload.question_id: case for case in discovered_cases}
            missing = [
                question_id
                for question_id in requested_question_ids
                if question_id not in case_by_question_id
            ]
            if missing:
                raise ValueError(
                    "The following question_id values were not found under "
                    f"{batch_metadata_dir}: {', '.join(missing)}"
                )
            metadata_cases = [case_by_question_id[question_id] for question_id in requested_question_ids]
        else:
            metadata_cases = discovered_cases
        selected_inputs = [case.input_payload for case in metadata_cases]
        input_label = str(batch_metadata_dir)
    else:
        if args.merge_only:
            raise ValueError("`--merge-only` requires `--metadata-dir`.")
        all_inputs = read_inputs(args.input_path)
        selected_inputs = select_inputs(all_inputs, requested_question_ids)
        batch_metadata_dir = (args.metadata_root / batch_timestamp).resolve()
        input_label = str(args.input_path)

    total = len(selected_inputs)
    print(f"[SCHEMA-LINKING-ONLY] selected={total} input={input_label}")
    if not args.quiet:
        print(f"[SCHEMA-LINKING-ONLY] engine_provider={args.engine_provider or '(default)'}")
        print(f"[SCHEMA-LINKING-ONLY] snapshot_mode={args.snapshot_mode}")
        print(f"[SCHEMA-LINKING-ONLY] output_filename={args.output_filename}")
    if requested_question_ids:
        if args.quiet:
            print("[SCHEMA-LINKING-ONLY] requested=" + ",".join(requested_question_ids))
        else:
            print(
                "[SCHEMA-LINKING-ONLY] requested question_id values: "
                + ", ".join(requested_question_ids)
            )

    if args.merge_only:
        batch_summary = run_merge_metadata_cases(cases=metadata_cases, args=args)
        succeeded_count = sum(1 for item in batch_summary if item.get("status") == "succeeded")
        failed_count = sum(1 for item in batch_summary if item.get("status") == "failed")
        coverage_payload = run_coverage_check(batch_metadata_dir=batch_metadata_dir, args=args)
        batch_payload: dict[str, Any] = {
            "timestamp": batch_timestamp,
            "mode": "schema_linking_merge_only",
            "snapshot_mode": args.snapshot_mode,
            "metadata_batch_dir": str(batch_metadata_dir),
            "selected_question_ids": [item.question_id for item in selected_inputs],
            "runs": batch_summary,
        }
        if coverage_payload:
            batch_payload["coverage"] = coverage_payload
        write_json(batch_metadata_dir / "schema_linking_merge_summary.json", batch_payload)
        print(
            f"[SCHEMA-LINKING-ONLY] merged {total} question(s): "
            f"succeeded={succeeded_count}, failed={failed_count}, "
            f"summary={batch_metadata_dir / 'schema_linking_merge_summary.json'}"
        )
        if not args.quiet:
            print(json.dumps(batch_payload, ensure_ascii=False, indent=2))
        return

    completed_count = 0
    should_run_parallel = args.max_workers > 1 and total > 1

    if should_run_parallel:
        worker_count = min(args.max_workers, total)
        if not args.quiet:
            print(f"[SCHEMA-LINKING-ONLY] running in parallel with max_workers={worker_count}")
        batch_summary_slots: list[dict[str, object] | None] = [None] * total
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {}
            for index, input_payload in enumerate(selected_inputs, start=1):
                if not args.quiet:
                    print(
                        f"[SCHEMA-LINKING-ONLY] starting question {index}/{total}: "
                        f"{input_payload.question_id}"
                    )
                future = executor.submit(
                    run_single_question,
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                    metadata_dir_override=(
                        metadata_cases[index - 1].case_dir if metadata_cases else None
                    ),
                    log_relative_dir=(
                        metadata_cases[index - 1].relative_case_dir if metadata_cases else None
                    ),
                    source_input_path=(
                        metadata_cases[index - 1].source_input_path if metadata_cases else None
                    ),
                )
                future_map[future] = (index - 1, input_payload)

            for future in as_completed(future_map):
                result_index, input_payload = future_map[future]
                try:
                    run_summary = future.result()
                except Exception as exc:
                    if not args.quiet:
                        print(
                            f"[SCHEMA-LINKING-ONLY] unhandled failure for "
                            f"{input_payload.question_id}: {exc}"
                        )
                    run_summary = build_unhandled_schema_linking_case_summary(
                        input_payload=input_payload,
                        run_timestamp=batch_timestamp,
                        args=args,
                        error_message=str(exc),
                    )
                batch_summary_slots[result_index] = run_summary
                completed_count += 1
                if not args.quiet:
                    emit_batch_progress(
                        completed_count=completed_count,
                        total_count=total,
                        run_summary=run_summary,
                    )

        if any(item is None for item in batch_summary_slots):
            raise RuntimeError("Schema-linking batch summary collection incomplete.")
        batch_summary = [item for item in batch_summary_slots if item is not None]
    else:
        batch_summary: list[dict[str, object]] = []
        for index, input_payload in enumerate(selected_inputs, start=1):
            if not args.quiet:
                print(
                    f"[SCHEMA-LINKING-ONLY] starting question {index}/{total}: "
                    f"{input_payload.question_id}"
                )
            try:
                run_summary = run_single_question(
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                    metadata_dir_override=(
                        metadata_cases[index - 1].case_dir if metadata_cases else None
                    ),
                    log_relative_dir=(
                        metadata_cases[index - 1].relative_case_dir if metadata_cases else None
                    ),
                    source_input_path=(
                        metadata_cases[index - 1].source_input_path if metadata_cases else None
                    ),
                )
            except Exception as exc:
                if not args.quiet:
                    print(
                        f"[SCHEMA-LINKING-ONLY] unhandled failure for "
                        f"{input_payload.question_id}: {exc}"
                    )
                run_summary = build_unhandled_schema_linking_case_summary(
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                    error_message=str(exc),
                )
            batch_summary.append(run_summary)
            completed_count += 1
            if not args.quiet:
                emit_batch_progress(
                    completed_count=completed_count,
                    total_count=total,
                    run_summary=run_summary,
                )

    succeeded_count = sum(1 for item in batch_summary if item.get("status") == "succeeded")
    failed_count = sum(1 for item in batch_summary if item.get("status") == "failed")
    coverage_payload = run_coverage_check(batch_metadata_dir=batch_metadata_dir, args=args)
    batch_payload: dict[str, Any] = {
        "timestamp": batch_timestamp,
        "mode": "schema_linking_only",
        "snapshot_mode": args.snapshot_mode,
        "input_path": input_label,
        "metadata_batch_dir": str(batch_metadata_dir),
        "selected_question_ids": [item.question_id for item in selected_inputs],
        "max_workers": min(args.max_workers, total) if total else args.max_workers,
        "runs": batch_summary,
    }
    if coverage_payload:
        batch_payload["coverage"] = coverage_payload

    write_json(batch_metadata_dir / "schema_linking_batch_summary.json", batch_payload)
    print(
        f"[SCHEMA-LINKING-ONLY] completed {total} question(s): "
        f"succeeded={succeeded_count}, failed={failed_count}, "
        f"summary={batch_metadata_dir / 'schema_linking_batch_summary.json'}"
    )
    if not args.quiet:
        print("[SCHEMA-LINKING-ONLY] batch summary follows")
        print(json.dumps(batch_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
