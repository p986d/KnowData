from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
from dataclasses import dataclass
from pathlib import Path
import traceback

from src.run.er2data import (
    DEFAULT_ENGINE_SCRIPT,
    DEFAULT_ANALYSIS_FILENAME as DEFAULT_ER2DATA_ANALYSIS_FILENAME,
    DEFAULT_FINAL_QUERY_FILENAME as DEFAULT_ER2DATA_FINAL_QUERY_FILENAME,
    DEFAULT_LOG_ROOT as DEFAULT_ER2DATA_LOG_ROOT,
    DEFAULT_NL2SQL_ENGINE_SCRIPT,
    DEFAULT_OUTPUT_FILENAME as DEFAULT_ER2DATA_OUTPUT_FILENAME,
    DEFAULT_PROMPT_DIR as DEFAULT_ER2DATA_PROMPT_DIR,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SPIDER2_ROOT,
    ER2DataRunner,
)
from src.run.nl2er_2 import (
    DEFAULT_INPUT_PATH,
    DEFAULT_LOG_ROOT as DEFAULT_NL2ER_LOG_ROOT,
    DEFAULT_OUTPUT_FILENAME as DEFAULT_NL2ER_OUTPUT_FILENAME,
    DEFAULT_PROMPT_DIR as DEFAULT_NL2ER_PROMPT_DIR,
    ERSkeletonIntegrityError,
    NL2ER,
    NL2ERInput,
    build_output_payload as build_nl2er_output_payload,
)
from src.utils.run_log import build_timestamp, resolve_run_dir, write_json


DEFAULT_METADATA_ROOT = Path("metadata")


@dataclass(slots=True)
class PipelineInput:
    question_id: str
    user_intent: str
    db_id: str
    db_hint: str = ""
    external_knowledge: str = ""


def serialize_input_payload(input_payload: PipelineInput) -> dict[str, str]:
    return {
        "question_id": input_payload.question_id,
        "user_intent": input_payload.user_intent,
        "db_id": input_payload.db_id,
        "db_hint": input_payload.db_hint,
        "external_knowledge": input_payload.external_knowledge,
    }


def _parse_single_input(payload: object, *, input_path: Path, index: int | None = None) -> PipelineInput:
    if not isinstance(payload, dict):
        location = f"{input_path}[{index}]" if index is not None else str(input_path)
        raise ValueError(f"Expected JSON object at {location}, got {type(payload).__name__}")

    question_id = str(
        payload.get("question_id") or payload.get("instance_id") or payload.get("db_id") or ""
    ).strip()
    user_intent = str(payload.get("user_intent") or payload.get("instruction") or "").strip()
    db_id = str(payload.get("db_id") or "").strip()
    db_hint = payload.get("db_hint", "")
    external_knowledge = payload.get("external_knowledge", "")

    if not question_id:
        location = f"{input_path}[{index}]" if index is not None else str(input_path)
        raise ValueError(f"`question_id` is required in {location}")
    if not user_intent:
        location = f"{input_path}[{index}]" if index is not None else str(input_path)
        raise ValueError(f"`user_intent` is required in {location}")
    if not db_id:
        location = f"{input_path}[{index}]" if index is not None else str(input_path)
        raise ValueError(f"`db_id` is required in {location}")
    if not isinstance(db_hint, str):
        location = f"{input_path}[{index}]" if index is not None else str(input_path)
        raise ValueError(f"`db_hint` must be a string in {location}")
    if not isinstance(external_knowledge, str):
        location = f"{input_path}[{index}]" if index is not None else str(input_path)
        raise ValueError(f"`external_knowledge` must be a string in {location}")

    return PipelineInput(
        question_id=question_id,
        user_intent=user_intent,
        db_id=db_id,
        db_hint=db_hint,
        external_knowledge=external_knowledge,
    )


def read_pipeline_inputs(path: str | Path) -> list[PipelineInput]:
    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if isinstance(payload, dict):
        return [_parse_single_input(payload, input_path=input_path)]
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON object or JSON list at {input_path}, got {type(payload).__name__}")

    inputs: list[PipelineInput] = []
    seen_question_ids: set[str] = set()
    for index, item in enumerate(payload):
        parsed = _parse_single_input(item, input_path=input_path, index=index)
        if parsed.question_id in seen_question_ids:
            raise ValueError(f"Duplicate question_id `{parsed.question_id}` found in {input_path}")
        seen_question_ids.add(parsed.question_id)
        inputs.append(parsed)
    return inputs


def normalize_question_ids(values: list[str] | None) -> list[str]:
    if not values:
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in value.split(","):
            question_id = item.strip()
            if not question_id or question_id in seen:
                continue
            normalized.append(question_id)
            seen.add(question_id)
    return normalized


def select_inputs(all_inputs: list[PipelineInput], requested_question_ids: list[str]) -> list[PipelineInput]:
    if not requested_question_ids:
        return all_inputs

    input_by_question_id = {item.question_id: item for item in all_inputs}
    missing_question_ids = [
        question_id for question_id in requested_question_ids if question_id not in input_by_question_id
    ]
    if missing_question_ids:
        raise ValueError(
            "The following question_id values were not found in the input file: "
            + ", ".join(missing_question_ids)
        )

    return [input_by_question_id[question_id] for question_id in requested_question_ids]


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.") from exc

    if parsed < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.")
    return parsed


def parse_cli_bool(value: str) -> bool:
    normalized = str(value or "").strip().casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError(
        f"Expected `true` or `false`, got `{value}`."
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the NL2ER -> ER2Data pipeline for one or more questions from the input file."
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run. Supports multiple values, or a comma-separated list in one argument.",
    )
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    parser.add_argument("--nl2er-log-root", type=Path, default=DEFAULT_NL2ER_LOG_ROOT)
    parser.add_argument("--er2data-log-root", type=Path, default=DEFAULT_ER2DATA_LOG_ROOT)
    parser.add_argument("--nl2er-prompt-dir", type=Path, default=DEFAULT_NL2ER_PROMPT_DIR)
    parser.add_argument("--er2data-prompt-dir", type=Path, default=DEFAULT_ER2DATA_PROMPT_DIR)
    parser.add_argument("--nl2er-model-config", default=None)
    parser.add_argument("--question-model-config", default=None)
    parser.add_argument("--schema-link-model-config", default=None)
    parser.add_argument("--nl2sql-model-config", default=None)
    parser.add_argument(
        "--enable-nl2er-db-hint",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
        help="Whether to pass `db_hint` into NL2ER prompts.",
    )
    parser.add_argument(
        "--enable-er2data-db-hint",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
        help="Whether to pass `db_hint` into ER2Data NL2SQL and final-query assembly.",
    )
    parser.add_argument(
        "--enable-er2query-db-hint",
        type=parse_cli_bool,
        default=None,
        metavar="{true,false}",
        help="Whether to pass `db_hint` into ER2Query prompts. Defaults to `--enable-er2data-db-hint` when omitted.",
    )
    parser.add_argument(
        "--exclude-desc-in-er2query",
        action="store_true",
        help="Do not pass target-unit desc into the ER2Query prompt.",
    )
    parser.add_argument("--include-conditions-in-er2query", action="store_true")
    parser.add_argument("--include-conditions-in-sql2nl", action="store_true")
    parser.add_argument("--max-batch-workers", type=positive_int, default=4)
    parser.add_argument("--max-question-concurrency", type=int, default=None)
    parser.add_argument("--skip-schema-linking", action="store_true")
    parser.add_argument("--max-linking-workers", type=int, default=4)
    parser.add_argument("--skip-nl2sql", action="store_true")
    parser.add_argument("--max-nl2sql-workers", type=int, default=1)
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
    return parser.parse_args()


def build_case_summary(
    *,
    input_payload: PipelineInput,
    run_id: str,
    run_timestamp: str,
    metadata_dir: Path,
    nl2er_log_dir: Path,
    er2data_log_dir: Path,
    nl2er_output_path: Path,
    er2data_output_path: Path,
    er2data_analysis_output_path: Path,
    er2data_final_query_output_path: Path,
    status: str,
    stage: str,
    nl2er_elapsed_seconds: float | None = None,
    er2data_summary: dict[str, object] | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "run_id": run_id,
        "question_id": input_payload.question_id,
        "db_id": input_payload.db_id,
        "timestamp": run_timestamp,
        "pipeline_input_path": "",
        "input_path": str(nl2er_output_path),
        "metadata_dir": str(metadata_dir),
        "nl2er_log_dir": str(nl2er_log_dir),
        "er2data_log_dir": str(er2data_log_dir),
        "output_path": str(er2data_output_path),
        "analysis_output_path": str(er2data_analysis_output_path),
        "final_query_output_path": str(er2data_final_query_output_path),
        "nl2er_output_path": str(nl2er_output_path),
        "er2data_output_path": str(er2data_output_path),
        "er2data_analysis_output_path": str(er2data_analysis_output_path),
        "er2data_final_query_output_path": str(er2data_final_query_output_path),
        "status": status,
        "stage": stage,
    }
    if nl2er_elapsed_seconds is not None:
        summary["nl2er_elapsed_seconds"] = float(nl2er_elapsed_seconds)
    if er2data_summary is not None:
        summary["er2data_summary"] = er2data_summary
    if error_message:
        summary["error_message"] = error_message
    return summary


def _resolve_case_dir_path(run_root: Path, *, question_id: str, run_timestamp: str) -> Path:
    resolved_root = run_root.expanduser()
    if not resolved_root.is_absolute():
        resolved_root = (Path.cwd() / resolved_root).resolve()
    else:
        resolved_root = resolved_root.resolve()
    return resolved_root / f"{question_id}_{run_timestamp}"


def _resolve_batch_case_dir_path(
    run_root: Path,
    *,
    batch_timestamp: str,
    question_id: str,
    run_timestamp: str,
) -> Path:
    return _resolve_case_dir_path(
        run_root=run_root / batch_timestamp,
        question_id=question_id,
        run_timestamp=run_timestamp,
    )


def build_unhandled_case_summary(
    *,
    input_payload: PipelineInput,
    run_timestamp: str,
    args: argparse.Namespace,
    error_message: str,
) -> dict[str, object]:
    metadata_dir = _resolve_batch_case_dir_path(
        args.metadata_root,
        batch_timestamp=run_timestamp,
        question_id=input_payload.question_id,
        run_timestamp=run_timestamp,
    )
    nl2er_log_dir = _resolve_batch_case_dir_path(
        args.nl2er_log_root,
        batch_timestamp=run_timestamp,
        question_id=input_payload.question_id,
        run_timestamp=run_timestamp,
    )
    er2data_log_dir = _resolve_batch_case_dir_path(
        args.er2data_log_root,
        batch_timestamp=run_timestamp,
        question_id=input_payload.question_id,
        run_timestamp=run_timestamp,
    )
    nl2er_output_path = metadata_dir / DEFAULT_NL2ER_OUTPUT_FILENAME
    er2data_output_path = metadata_dir / DEFAULT_ER2DATA_OUTPUT_FILENAME
    er2data_analysis_output_path = metadata_dir / DEFAULT_ER2DATA_ANALYSIS_FILENAME
    er2data_final_query_output_path = metadata_dir / DEFAULT_ER2DATA_FINAL_QUERY_FILENAME
    run_id = f"{input_payload.question_id}_{run_timestamp}"
    summary = build_case_summary(
        input_payload=input_payload,
        run_id=run_id,
        run_timestamp=run_timestamp,
        metadata_dir=metadata_dir,
        nl2er_log_dir=nl2er_log_dir,
        er2data_log_dir=er2data_log_dir,
        nl2er_output_path=nl2er_output_path,
        er2data_output_path=er2data_output_path,
        er2data_analysis_output_path=er2data_analysis_output_path,
        er2data_final_query_output_path=er2data_final_query_output_path,
        status="failed",
        stage="pipeline",
        error_message=error_message,
    )
    summary["pipeline_input_path"] = str(args.input_path)
    return annotate_db_hint_flags(summary, args=args)


def emit_batch_progress(
    *,
    completed_count: int,
    total_count: int,
    run_summary: dict[str, object],
) -> None:
    print(
        "[PIPELINE] progress "
        f"completed={completed_count}/{total_count} "
        f"question_id={run_summary.get('question_id', '')} "
        f"status={run_summary.get('status', 'unknown')} "
        f"stage={run_summary.get('stage', 'unknown')}"
    )


def annotate_db_hint_flags(
    summary: dict[str, object],
    *,
    args: argparse.Namespace,
) -> dict[str, object]:
    summary["enable_nl2er_db_hint"] = bool(args.enable_nl2er_db_hint)
    summary["enable_er2data_db_hint"] = bool(args.enable_er2data_db_hint)
    summary["enable_er2query_db_hint"] = bool(resolve_er2query_db_hint_flag(args))
    return summary


def resolve_er2query_db_hint_flag(args: argparse.Namespace) -> bool:
    if args.enable_er2query_db_hint is None:
        return bool(args.enable_er2data_db_hint)
    return bool(args.enable_er2query_db_hint)


def run_single_question(
    *,
    input_payload: PipelineInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    run_id = f"{input_payload.question_id}_{run_timestamp}"

    metadata_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=None,
        run_root=args.metadata_root / run_timestamp,
        timestamp=run_timestamp,
    )
    nl2er_log_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=None,
        run_root=args.nl2er_log_root / run_timestamp,
        timestamp=run_timestamp,
    )
    er2data_log_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=None,
        run_root=args.er2data_log_root / run_timestamp,
        timestamp=run_timestamp,
    )

    nl2er_output_path = metadata_dir / DEFAULT_NL2ER_OUTPUT_FILENAME
    er2data_output_path = metadata_dir / DEFAULT_ER2DATA_OUTPUT_FILENAME
    er2data_analysis_output_path = metadata_dir / DEFAULT_ER2DATA_ANALYSIS_FILENAME
    er2data_final_query_output_path = metadata_dir / DEFAULT_ER2DATA_FINAL_QUERY_FILENAME

    serialized_input = serialize_input_payload(input_payload)
    nl2er_db_hint = input_payload.db_hint if args.enable_nl2er_db_hint else ""
    er2data_db_hint = input_payload.db_hint if args.enable_er2data_db_hint else ""
    enable_er2query_db_hint = resolve_er2query_db_hint_flag(args)
    nl2er_serialized_input = {
        **serialized_input,
        "db_hint": nl2er_db_hint,
    }
    write_json(
        metadata_dir / "input.json",
        {
            **serialized_input,
            "input_path": str(args.input_path),
        },
    )
    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": input_payload.question_id,
            "db_id": input_payload.db_id,
            "timestamp": run_timestamp,
            "pipeline_input_path": str(args.input_path),
            "input_path": str(nl2er_output_path),
            "metadata_dir": str(metadata_dir),
            "nl2er_log_dir": str(nl2er_log_dir),
            "er2data_log_dir": str(er2data_log_dir),
            "output_path": str(er2data_output_path),
            "analysis_output_path": str(er2data_analysis_output_path),
            "final_query_output_path": str(er2data_final_query_output_path),
            "nl2er_output_path": str(nl2er_output_path),
            "er2data_output_path": str(er2data_output_path),
            "er2data_analysis_output_path": str(er2data_analysis_output_path),
            "er2data_final_query_output_path": str(er2data_final_query_output_path),
            "engine_provider": args.engine_provider,
            "enable_nl2er_db_hint": args.enable_nl2er_db_hint,
            "enable_er2data_db_hint": args.enable_er2data_db_hint,
            "enable_er2query_db_hint": enable_er2query_db_hint,
            "include_desc_in_er2query": not args.exclude_desc_in_er2query,
            "include_conditions_in_er2query": args.include_conditions_in_er2query,
            "include_conditions_in_sql2nl": args.include_conditions_in_sql2nl,
        },
    )

    print(f"[PIPELINE] run_id={run_id}")
    print(f"[PIPELINE] question_id={input_payload.question_id}")
    print(f"[PIPELINE] db_id={input_payload.db_id}")
    print(f"[PIPELINE] metadata_dir={metadata_dir}")
    print(f"[PIPELINE] nl2er_log_dir={nl2er_log_dir}")
    print(f"[PIPELINE] er2data_log_dir={er2data_log_dir}")
    print(f"[PIPELINE] enable_nl2er_db_hint={args.enable_nl2er_db_hint}")
    print(f"[PIPELINE] enable_er2data_db_hint={args.enable_er2data_db_hint}")
    print(f"[PIPELINE] enable_er2query_db_hint={enable_er2query_db_hint}")

    nl2er_input = NL2ERInput(
        question_id=input_payload.question_id,
        user_intent=input_payload.user_intent,
        db_id=input_payload.db_id,
        db_hint=nl2er_db_hint,
        external_knowledge=input_payload.external_knowledge,
    )
    write_json(metadata_dir / "nl2er_input.json", nl2er_serialized_input)
    write_json(nl2er_log_dir / "input.json", nl2er_serialized_input)

    nl2er = NL2ER(
        prompt_dir=args.nl2er_prompt_dir,
        log_dir=nl2er_log_dir,
        input_payload=nl2er_input,
        model_config=args.nl2er_model_config,
    )
    try:
        nl2er_payload = nl2er.run()
    except ERSkeletonIntegrityError as exc:
        write_json(
            nl2er_output_path,
            build_nl2er_output_payload(
                input_payload=nl2er_input,
                er_result={
                    **exc.result,
                    "integrity_report": exc.integrity_report,
                },
            ),
        )
        print(f"[PIPELINE] nl2er integrity failed for {input_payload.question_id}: {exc}")
        run_summary = build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            nl2er_log_dir=nl2er_log_dir,
            er2data_log_dir=er2data_log_dir,
            nl2er_output_path=nl2er_output_path,
            er2data_output_path=er2data_output_path,
            er2data_analysis_output_path=er2data_analysis_output_path,
            er2data_final_query_output_path=er2data_final_query_output_path,
            status="integrity_failed",
            stage="nl2er",
            error_message=str(exc),
        )
        run_summary["pipeline_input_path"] = str(args.input_path)
        annotate_db_hint_flags(run_summary, args=args)
        write_json(metadata_dir / "run_context.json", run_summary)
        write_json(
            nl2er_log_dir / "error.json",
            {
                "run_id": run_id,
                "question_id": input_payload.question_id,
                "db_id": input_payload.db_id,
                "stage": "nl2er",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "integrity_report": exc.integrity_report,
            },
        )
        return run_summary
    except Exception as exc:
        print(f"[PIPELINE] nl2er failed for {input_payload.question_id}: {exc}")
        run_summary = build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            nl2er_log_dir=nl2er_log_dir,
            er2data_log_dir=er2data_log_dir,
            nl2er_output_path=nl2er_output_path,
            er2data_output_path=er2data_output_path,
            er2data_analysis_output_path=er2data_analysis_output_path,
            er2data_final_query_output_path=er2data_final_query_output_path,
            status="failed",
            stage="nl2er",
            error_message=str(exc),
        )
        run_summary["pipeline_input_path"] = str(args.input_path)
        annotate_db_hint_flags(run_summary, args=args)
        write_json(metadata_dir / "run_context.json", run_summary)
        write_json(
            nl2er_log_dir / "error.json",
            {
                "run_id": run_id,
                "question_id": input_payload.question_id,
                "db_id": input_payload.db_id,
                "stage": "nl2er",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        return run_summary
    write_json(
        nl2er_output_path,
        build_nl2er_output_payload(
            input_payload=nl2er_input,
            er_result=nl2er_payload["result"],
        ),
    )
    print(f"[PIPELINE] nl2er output wrote to {nl2er_output_path}")

    runner = ER2DataRunner(
        prompt_dir=args.er2data_prompt_dir,
        log_dir=er2data_log_dir,
        question_model_config=args.question_model_config,
        schema_link_model_config=args.schema_link_model_config,
        nl2sql_model_config=args.nl2sql_model_config,
        include_desc_in_er2query=not args.exclude_desc_in_er2query,
        include_conditions_in_er2query=args.include_conditions_in_er2query,
        include_conditions_in_sql2nl=args.include_conditions_in_sql2nl,
    )
    try:
        er2data_payload = runner.run(
            input_path=nl2er_output_path,
            output_path=er2data_output_path,
            db_id=input_payload.db_id,
            db_hint=er2data_db_hint,
            enable_db_hint=args.enable_er2data_db_hint,
            enable_er2query_db_hint=enable_er2query_db_hint,
            external_knowledge=input_payload.external_knowledge,
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
        )
    except Exception as exc:
        print(f"[PIPELINE] er2data failed for {input_payload.question_id}: {exc}")
        run_summary = build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            nl2er_log_dir=nl2er_log_dir,
            er2data_log_dir=er2data_log_dir,
            nl2er_output_path=nl2er_output_path,
            er2data_output_path=er2data_output_path,
            er2data_analysis_output_path=er2data_analysis_output_path,
            er2data_final_query_output_path=er2data_final_query_output_path,
            status="failed",
            stage="er2data",
            nl2er_elapsed_seconds=nl2er_payload["elapsed_seconds"],
            error_message=str(exc),
        )
        run_summary["pipeline_input_path"] = str(args.input_path)
        annotate_db_hint_flags(run_summary, args=args)
        write_json(metadata_dir / "run_context.json", run_summary)
        write_json(
            er2data_log_dir / "error.json",
            {
                "run_id": run_id,
                "question_id": input_payload.question_id,
                "db_id": input_payload.db_id,
                "stage": "er2data",
                "error_type": type(exc).__name__,
                "error_message": str(exc),
                "traceback": traceback.format_exc(),
            },
        )
        return run_summary
    print(f"[PIPELINE] er2data output wrote to {er2data_output_path}")
    print(f"[PIPELINE] er2data analysis wrote to {er2data_payload['analysis_output_path']}")
    print(f"[PIPELINE] er2data final query wrote to {er2data_payload['final_query_output_path']}")

    run_summary = build_case_summary(
        input_payload=input_payload,
        run_id=run_id,
        run_timestamp=run_timestamp,
        metadata_dir=metadata_dir,
        nl2er_log_dir=nl2er_log_dir,
        er2data_log_dir=er2data_log_dir,
        nl2er_output_path=nl2er_output_path,
        er2data_output_path=Path(er2data_payload["output_path"]),
        er2data_analysis_output_path=Path(er2data_payload["analysis_output_path"]),
        er2data_final_query_output_path=Path(er2data_payload["final_query_output_path"]),
        status="succeeded",
        stage="completed",
        nl2er_elapsed_seconds=nl2er_payload["elapsed_seconds"],
        er2data_summary=er2data_payload,
    )
    run_summary["pipeline_input_path"] = str(args.input_path)
    run_summary["engine_provider"] = args.engine_provider
    run_summary["enable_nl2er_db_hint"] = args.enable_nl2er_db_hint
    run_summary["enable_er2data_db_hint"] = args.enable_er2data_db_hint
    run_summary["enable_er2query_db_hint"] = enable_er2query_db_hint
    run_summary["include_desc_in_er2query"] = not args.exclude_desc_in_er2query
    run_summary["include_conditions_in_er2query"] = args.include_conditions_in_er2query
    run_summary["include_conditions_in_sql2nl"] = args.include_conditions_in_sql2nl
    write_json(metadata_dir / "run_context.json", run_summary)
    return run_summary


def main() -> None:
    args = parse_args()

    all_inputs = read_pipeline_inputs(args.input_path)
    requested_question_ids = normalize_question_ids(args.question_id)
    selected_inputs = select_inputs(all_inputs, requested_question_ids)

    print(
        f"[PIPELINE] selected {len(selected_inputs)} question(s) from {args.input_path}"
    )
    if requested_question_ids:
        print(f"[PIPELINE] requested question_id values: {', '.join(requested_question_ids)}")

    batch_timestamp = build_timestamp()
    total = len(selected_inputs)
    completed_count = 0
    should_run_parallel = args.max_batch_workers > 1 and total > 1

    if should_run_parallel:
        worker_count = min(args.max_batch_workers, total)
        print(f"[PIPELINE] running in parallel with max_batch_workers={worker_count}")
        batch_summary_slots: list[dict[str, object] | None] = [None] * total
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {}
            for index, input_payload in enumerate(selected_inputs, start=1):
                print(f"[PIPELINE] starting question {index}/{total}: {input_payload.question_id}")
                future = executor.submit(
                    run_single_question,
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                )
                future_map[future] = (index - 1, input_payload)

            for future in as_completed(future_map):
                result_index, input_payload = future_map[future]
                try:
                    run_summary = future.result()
                except Exception as exc:
                    print(f"[PIPELINE] unhandled failure for {input_payload.question_id}: {exc}")
                    run_summary = build_unhandled_case_summary(
                        input_payload=input_payload,
                        run_timestamp=batch_timestamp,
                        args=args,
                        error_message=str(exc),
                    )
                batch_summary_slots[result_index] = run_summary
                completed_count += 1
                emit_batch_progress(
                    completed_count=completed_count,
                    total_count=total,
                    run_summary=run_summary,
                )

        if any(item is None for item in batch_summary_slots):
            raise RuntimeError("Pipeline batch summary collection incomplete.")
        batch_summary = [item for item in batch_summary_slots if item is not None]
    else:
        batch_summary: list[dict[str, object]] = []
        for index, input_payload in enumerate(selected_inputs, start=1):
            print(f"[PIPELINE] starting question {index}/{total}: {input_payload.question_id}")
            try:
                run_summary = run_single_question(
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                )
            except Exception as exc:
                print(f"[PIPELINE] unhandled failure for {input_payload.question_id}: {exc}")
                run_summary = build_unhandled_case_summary(
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                    error_message=str(exc),
                )
            batch_summary.append(run_summary)
            completed_count += 1
            emit_batch_progress(
                completed_count=completed_count,
                total_count=total,
                run_summary=run_summary,
            )

    succeeded_count = sum(1 for item in batch_summary if item.get("status") == "succeeded")
    failed_count = total - succeeded_count
    batch_payload = {
        "timestamp": batch_timestamp,
        "input_path": str(args.input_path),
        "selected_question_ids": [item.question_id for item in selected_inputs],
        "max_batch_workers": min(args.max_batch_workers, total) if total else args.max_batch_workers,
        "runs": batch_summary,
    }
    print(
        f"[PIPELINE] completed {total} question(s): "
        f"succeeded={succeeded_count}, failed={failed_count}"
    )
    print("[PIPELINE] batch summary follows")
    print(json.dumps(batch_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
