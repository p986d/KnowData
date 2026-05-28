import argparse
import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.llm.reasoning import REASONING_MODE_MAP
from src.er2data.er2query import (
    DEFAULT_DB_ID,
    DEFAULT_ER2QUERY_TEMPLATE_NAME,
    DEFAULT_OUTPUT_FILENAME,
    DEFAULT_PROMPT_DIR,
    ER2DataRunner,
    parse_cli_bool,
    read_json,
    read_question_id,
    read_sidecar_context,
    write_json,
)
from src.utils.run_log import build_timestamp, emit_step_done_log


DEFAULT_METADATA_DIR = Path("metadata")
DEFAULT_LOG_ROOT = Path("log/er2data_batch")
DEFAULT_INPUT_FILENAME = "nl2er_output.json"
DEFAULT_BATCH_SUMMARY_FILENAME = "er2data_batch_summary.json"


@dataclass(slots=True)
class CaseInput:
    input_path: Path
    case_dir: Path
    relative_case_dir: Path


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
        return (Path.cwd() / resolved).resolve()
    return resolved.resolve()


def discover_case_inputs(metadata_dir: str | Path) -> tuple[Path, list[CaseInput]]:
    root_dir = resolve_path(metadata_dir)
    if not root_dir.exists():
        raise FileNotFoundError(f"Metadata path does not exist: {root_dir}")

    if root_dir.is_file():
        if root_dir.name != DEFAULT_INPUT_FILENAME:
            raise ValueError(
                f"Expected `{DEFAULT_INPUT_FILENAME}` when a file path is provided, "
                f"got `{root_dir.name}`."
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


def resolve_question_id(case_input: CaseInput) -> str:
    sidecar_context = read_sidecar_context(case_input.input_path)
    return (
        str(sidecar_context.get("question_id") or "").strip()
        or read_question_id(case_input.input_path)
        or case_input.case_dir.name
    )


def resolve_db_id(case_input: CaseInput, fallback_db_id: str) -> str:
    sidecar_context = read_sidecar_context(case_input.input_path)
    db_id = str(sidecar_context.get("db_id") or "").strip()
    if db_id:
        return db_id

    try:
        payload = read_json(case_input.input_path)
    except Exception:
        payload = {}
    db_id = str(payload.get("db_id") or "").strip()
    return db_id or fallback_db_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run ER2Data for one metadata case directory or a metadata batch "
            "directory containing multiple nl2er_output.json files."
        )
    )
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--db-id", default=DEFAULT_DB_ID)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--question-model-config", default=None)
    parser.add_argument("--schema-link-model-config", default=None)
    parser.add_argument("--nl2sql-model-config", default=None)
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
    )
    parser.add_argument(
        "--enable-er2query-db-hint",
        type=parse_cli_bool,
        default=None,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--er2query-template-name",
        default=DEFAULT_ER2QUERY_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--exclude-desc-in-er2query",
        type=parse_cli_bool,
        default=False,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--include-conditions-in-er2query",
        type=parse_cli_bool,
        default=False,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--include-conditions-in-sql2nl",
        type=parse_cli_bool,
        default=False,
        metavar="{true,false}",
    )
    parser.add_argument("--max-batch-workers", type=positive_int, default=4)
    parser.add_argument("--max-question-concurrency", type=int, default=None)
    parser.add_argument("--skip-schema-linking", action="store_true")
    parser.add_argument("--max-linking-workers", type=positive_int, default=4)
    parser.add_argument("--skip-nl2sql", action="store_true")
    parser.add_argument("--max-nl2sql-workers", type=positive_int, default=1)
    parser.add_argument("--engine-provider", default=None)
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument("--reforce-root", type=Path, default=None)
    parser.add_argument("--engine-script", type=Path, default=None)
    parser.add_argument("--nl2sql-engine-script", type=Path, default=None)
    parser.add_argument("--schema-link-timeout-seconds", type=int, default=None)
    parser.add_argument("--nl2sql-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--schema-link-temperature", type=float, default=0.0)
    parser.add_argument("--shortlist-trigger", type=int, default=18)
    parser.add_argument("--max-shortlist-tables", type=int, default=24)
    parser.add_argument("--sample-row-limit", type=int, default=2)
    parser.add_argument("--sample-value-max-chars", type=int, default=300)
    parser.add_argument("--similar-tables-hint-limit", type=int, default=12)
    parser.add_argument("--merge-connections-into-relationships", action="store_true")
    return parser.parse_args()


def run_single_case(
    *,
    case_input: CaseInput,
    batch_root: Path,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, Any]:
    case_started_at = time.perf_counter()
    sidecar_context = read_sidecar_context(case_input.input_path)
    question_id = resolve_question_id(case_input)
    db_id = resolve_db_id(case_input, str(args.db_id or DEFAULT_DB_ID).strip())
    output_path = case_input.case_dir / DEFAULT_OUTPUT_FILENAME
    log_dir = resolve_path(args.log_root) / run_timestamp / case_input.relative_case_dir
    resolved_enable_er2query_db_hint = (
        args.enable_nl2sql_db_hint
        if args.enable_er2query_db_hint is None
        else args.enable_er2query_db_hint
    )

    runner = ER2DataRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        er2query_template_name=args.er2query_template_name,
        question_model_config=args.question_model_config,
        schema_link_model_config=args.schema_link_model_config,
        nl2sql_model_config=args.nl2sql_model_config,
        reasoning_mode=args.reasoning_mode,
        include_desc_in_er2query=not args.exclude_desc_in_er2query,
        include_conditions_in_er2query=args.include_conditions_in_er2query,
        include_conditions_in_sql2nl=args.include_conditions_in_sql2nl,
        write_wrapper_logs=False,
        create_schema_linking_log_dirs=False,
    )
    payload = runner.run(
        input_path=case_input.input_path,
        output_path=output_path,
        db_id=db_id,
        db_hint=str(sidecar_context.get("db_hint") or "").strip(),
        external_knowledge=str(sidecar_context.get("external_knowledge") or "").strip(),
        enable_db_hint=args.enable_nl2sql_db_hint,
        enable_er2query_db_hint=resolved_enable_er2query_db_hint,
        max_question_concurrency=args.max_question_concurrency,
        skip_schema_linking=args.skip_schema_linking,
        max_linking_workers=args.max_linking_workers,
        skip_nl2sql=args.skip_nl2sql,
        max_nl2sql_workers=args.max_nl2sql_workers,
        engine_provider=args.engine_provider,
        spider2_root=args.spider2_root,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
        nl2sql_engine_script=args.nl2sql_engine_script,
        schema_link_timeout_seconds=args.schema_link_timeout_seconds,
        nl2sql_timeout_seconds=args.nl2sql_timeout_seconds,
        schema_link_temperature=args.schema_link_temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.sample_row_limit,
        sample_value_max_chars=args.sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        merge_connections_into_relationships=args.merge_connections_into_relationships,
    )

    summary = {
        "ok": True,
        "question_id": question_id,
        "db_id": db_id,
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "input_path": str(case_input.input_path),
        "output_path": str(output_path),
        "analysis_output_path": str(payload["analysis_output_path"]),
        "final_query_output_path": str(payload["final_query_output_path"]),
        "log_dir": str(log_dir),
        "engine_provider": args.engine_provider,
        "batch_root": str(batch_root),
        "elapsed_seconds": round(time.perf_counter() - case_started_at, 3),
        "summary": payload,
    }
    write_json(case_input.case_dir / "er2data_run_context.json", summary)
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
        try:
            question_id = resolve_question_id(case_input)
        except Exception:
            question_id = case_input.case_dir.name
        try:
            db_id = resolve_db_id(case_input, str(args.db_id or DEFAULT_DB_ID).strip())
        except Exception:
            db_id = str(args.db_id or DEFAULT_DB_ID).strip()

        failure_payload = {
            "ok": False,
            "question_id": question_id,
            "db_id": db_id,
            "case_dir": str(case_input.case_dir),
            "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
            "input_path": str(case_input.input_path),
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "batch_root": str(batch_root),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(output_path, failure_payload)
        write_json(case_input.case_dir / "er2data_run_context.json", failure_payload)
        write_json(log_dir / "error.json", failure_payload)
        return failure_payload


def main() -> None:
    args = parse_args()
    batch_root, case_inputs = discover_case_inputs(args.metadata_dir)
    run_timestamp = build_timestamp()
    batch_started_at = time.perf_counter()

    print(f"[ER2DATA_BATCH] metadata_dir={resolve_path(args.metadata_dir)}")
    print(f"[ER2DATA_BATCH] discovered_cases={len(case_inputs)}")
    print(f"[ER2DATA_BATCH] run_timestamp={run_timestamp}")
    print(f"[ER2DATA_BATCH] engine_provider={args.engine_provider}")
    print(f"[ER2DATA_BATCH] max_batch_workers={args.max_batch_workers}")
    print(f"[ER2DATA_BATCH] max_linking_workers={args.max_linking_workers}")
    print(f"[ER2DATA_BATCH] max_nl2sql_workers={args.max_nl2sql_workers}")
    print(f"[ER2DATA_BATCH] prompt_dir={resolve_path(args.prompt_dir)}")
    print(f"[ER2DATA_BATCH] log_root={resolve_path(args.log_root)}")
    if args.reasoning_mode:
        print(f"[ER2DATA_BATCH] reasoning_mode={args.reasoning_mode}")

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
                "[ER2DATA_BATCH] progress "
                f"completed={len(completed_summaries)}/{len(case_inputs)} "
                f"question_id={summary.get('question_id', '')} "
                f"ok={summary.get('ok', False)}"
            )

    success_count = sum(1 for item in completed_summaries if item.get("ok"))
    batch_payload = {
        "timestamp": run_timestamp,
        "metadata_dir": str(batch_root),
        "max_batch_workers": worker_count,
        "case_count": len(case_inputs),
        "ok_cases": success_count,
        "failed_cases": len(case_inputs) - success_count,
        "runs": completed_summaries,
    }
    write_json(batch_root / DEFAULT_BATCH_SUMMARY_FILENAME, batch_payload)
    emit_step_done_log(
        prefix="ER2DATA_BATCH",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        cases=len(case_inputs),
        ok_cases=success_count,
        failed_cases=len(case_inputs) - success_count,
    )
    print("[ER2DATA_BATCH] batch summary follows")
    print(json.dumps(batch_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
