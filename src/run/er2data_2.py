from __future__ import annotations

import argparse
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.llm.reasoning import REASONING_MODE_MAP
from src.nl2sql.defaults import (
    DEFAULT_ENGINE_SCRIPT,
    DEFAULT_REFORCE_ROOT,
)
from src.nl2sql.registry import resolve_engine_runtime
from src.run.er2data import (
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
    write_json,
)
from src.utils.run_log import build_timestamp, emit_step_done_log


DEFAULT_METADATA_DIR = Path("src/metadata")
DEFAULT_LOG_ROOT = Path("log/er2data_2")
DEFAULT_INPUT_FILENAME = "nl2er_output.json"
DEFAULT_OUTPUT_FILENAME = "schema_linking.json"
SCHEMA_LINK_TARGET_UNIT_TYPES = {"entity", "relationship"}


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
            fullname = str(item.get("fullname") or "").strip()
        else:
            fullname = str(item or "").strip()
        if fullname:
            fullnames.append(fullname)
    return fullnames


def build_compact_schema_linking_output(
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

        schema_linking = unit_result.get("schema_linking", {})
        question_generation = unit_result.get("question_generation", {})
        payload = {
            "db_id": db_id,
            "question": str(unit_result.get("question") or ""),
            "linked_tables": extract_fullnames(
                schema_linking.get("tables", []) if isinstance(schema_linking, dict) else []
            ),
            "linked_columns": extract_fullnames(
                schema_linking.get("columns", []) if isinstance(schema_linking, dict) else []
            ),
        }
        error = ""
        if isinstance(question_generation, dict):
            error = str(question_generation.get("error") or "").strip()
        if not error and isinstance(schema_linking, dict):
            error = str(schema_linking.get("error") or "").strip()
        if error:
            payload["error"] = error
        output[unit_name] = payload
    return output


class ER2DataSchemaLinkRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        er2query_template_name: str = DEFAULT_ER2QUERY_TEMPLATE_NAME,
        question_model_config: str | None = None,
        schema_link_model_config: str | None = None,
        reasoning_mode: str | None = None,
        include_desc_in_er2query: bool = True,
        include_conditions_in_er2query: bool = False,
    ) -> None:
        self.runner = ER2DataRunner(
            prompt_dir=prompt_dir,
            log_dir=log_dir,
            er2query_template_name=er2query_template_name,
            question_model_config=question_model_config,
            schema_link_model_config=schema_link_model_config,
            nl2sql_model_config=schema_link_model_config,
            reasoning_mode=reasoning_mode,
            include_desc_in_er2query=include_desc_in_er2query,
            include_conditions_in_er2query=include_conditions_in_er2query,
            include_conditions_in_sql2nl=False,
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
        max_question_concurrency: int | None,
        max_linking_workers: int,
        schema_link_timeout_seconds: int | None,
        schema_link_temperature: float,
        shortlist_trigger: int,
        max_shortlist_tables: int,
        sample_row_limit: int,
        sample_value_max_chars: int,
        similar_tables_hint_limit: int,
        db_hint: str = "",
        enable_er2query_db_hint: bool = True,
        external_knowledge: str = "",
    ) -> dict[str, Any]:
        runtime = resolve_engine_runtime(
            provider_name=engine_provider,
            spider2_root=spider2_root,
            reforce_root=reforce_root,
            engine_script=engine_script,
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

        prompt_context = resolve_er2query_prompt_context(
            er_model=er_model,
            input_path=input_path,
            db_hint=db_hint,
            enable_db_hint=enable_er2query_db_hint,
            external_knowledge=resolved_external_knowledge,
        )
        resolved_db_hint = resolve_db_hint(
            er_model=er_model,
            input_path=input_path,
            db_hint=db_hint,
            enable_db_hint=enable_er2query_db_hint,
        )

        all_units = self.runner.build_units(er_model)
        step_started_at = time.perf_counter()
        unit_results = self.runner.generate_questions(
            all_units,
            prompt_context=prompt_context,
            max_concurrency=max_question_concurrency,
        )
        schema_link_target_unit_results = [
            item
            for item in unit_results
            if str(item.get("target_unit_type") or "").strip() in SCHEMA_LINK_TARGET_UNIT_TYPES
        ]
        question_success_count = sum(
            1 for item in unit_results if item["question_generation"]["ok"]
        )
        schema_link_target_question_success_count = sum(
            1
            for item in schema_link_target_unit_results
            if item["question_generation"]["ok"]
        )
        skipped_unit_count = max(0, len(all_units) - len(schema_link_target_unit_results))
        emit_step_done_log(
            prefix="ER2DATA_2",
            step="generate_questions",
            elapsed_seconds=time.perf_counter() - step_started_at,
            units=len(all_units),
            questions=question_success_count,
            schema_link_target_questions=schema_link_target_question_success_count,
        )

        step_started_at = time.perf_counter()
        if schema_link_target_unit_results:
            self.runner.run_schema_linking_batch(
                schema_link_target_unit_results,
                db_id=db_id,
                max_workers=max_linking_workers,
                provider_name=runtime.provider_name,
                runtime=runtime,
                timeout_seconds=schema_link_timeout_seconds,
                temperature=schema_link_temperature,
                shortlist_trigger=shortlist_trigger,
                max_shortlist_tables=max_shortlist_tables,
                sample_row_limit=sample_row_limit,
                sample_value_max_chars=sample_value_max_chars,
                similar_tables_hint_limit=similar_tables_hint_limit,
            )

        schema_link_success_count = sum(
            1
            for item in unit_results
            if isinstance(item.get("schema_linking"), dict)
            and item["schema_linking"].get("ok")
        )
        emit_step_done_log(
            prefix="ER2DATA_2",
            step="run_schema_linking_batch",
            elapsed_seconds=time.perf_counter() - step_started_at,
            units=len(schema_link_target_unit_results),
            workers=max(1, max_linking_workers),
            schema_link_ok=schema_link_success_count,
        )

        ok = (
            question_success_count == len(all_units)
            and schema_link_target_question_success_count == len(schema_link_target_unit_results)
            and schema_link_success_count == len(schema_link_target_unit_results)
        )
        metadata_payload = build_compact_schema_linking_output(
            db_id=db_id,
            unit_results=schema_link_target_unit_results,
        )
        write_json(output_path, metadata_payload)
        return {
            "ok": ok,
            "question_id": read_question_id(input_path),
            "db_id": db_id,
            "input_path": str(Path(input_path).resolve()),
            "output_path": str(resolve_path(output_path)),
            "engine_provider": runtime.provider_name,
            "prompt_context": {
                "user_intent": str(prompt_context.get("user_intent") or ""),
                "db_hint": resolved_db_hint,
                "external_knowledge": str(prompt_context.get("external_knowledge") or ""),
            },
            "summary": {
                "total_unit_count": len(all_units),
                "processed_unit_count": len(all_units),
                "schema_link_target_unit_count": len(schema_link_target_unit_results),
                "skipped_unit_count": skipped_unit_count,
                "question_success_count": question_success_count,
                "schema_link_target_question_success_count": (
                    schema_link_target_question_success_count
                ),
                "schema_link_success_count": schema_link_success_count,
            },
            "metadata_payload_keys": sorted(metadata_payload.keys()),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read one metadata case directory or a metadata batch directory, generate "
            "ER2Query questions for all ER units, and run schema linking for entity "
            "and relationship units without NL2SQL."
        )
    )
    parser.add_argument("--metadata-dir", type=Path, default=DEFAULT_METADATA_DIR)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--question-model-config", default=None)
    parser.add_argument("--schema-link-model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument(
        "--enable-er2query-db-hint",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--er2query-template-name",
        default=DEFAULT_ER2QUERY_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--exclude-desc-in-er2query",
        action="store_true",
        help="Do not pass target-unit desc into the ER2Query prompt.",
    )
    parser.add_argument("--include-conditions-in-er2query", action="store_true")
    parser.add_argument("--max-batch-workers", type=positive_int, default=4)
    parser.add_argument("--max-question-concurrency", type=int, default=None)
    parser.add_argument("--max-linking-workers", type=positive_int, default=4)
    parser.add_argument("--engine-provider", default="reforce_gen_sl_m1")
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument("--reforce-root", type=Path, default=DEFAULT_REFORCE_ROOT)
    parser.add_argument("--engine-script", type=Path, default=DEFAULT_ENGINE_SCRIPT)
    parser.add_argument("--schema-link-timeout-seconds", type=int, default=None)
    parser.add_argument("--schema-link-temperature", type=float, default=0.0)
    parser.add_argument("--shortlist-trigger", type=int, default=18)
    parser.add_argument("--max-shortlist-tables", type=int, default=24)
    parser.add_argument("--sample-row-limit", type=int, default=2)
    parser.add_argument("--sample-value-max-chars", type=int, default=300)
    parser.add_argument("--similar-tables-hint-limit", type=int, default=12)
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

    runner = ER2DataSchemaLinkRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        er2query_template_name=args.er2query_template_name,
        question_model_config=args.question_model_config,
        schema_link_model_config=args.schema_link_model_config,
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
        max_question_concurrency=args.max_question_concurrency,
        max_linking_workers=args.max_linking_workers,
        schema_link_timeout_seconds=args.schema_link_timeout_seconds,
        schema_link_temperature=args.schema_link_temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.sample_row_limit,
        sample_value_max_chars=args.sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        db_hint=str(sidecar_context.get("db_hint") or "").strip(),
        enable_er2query_db_hint=bool(args.enable_er2query_db_hint),
        external_knowledge=str(sidecar_context.get("external_knowledge") or "").strip(),
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
    print(f"[ER2DATA_2] max_linking_workers={args.max_linking_workers}")
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
