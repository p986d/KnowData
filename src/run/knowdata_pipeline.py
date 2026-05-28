from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
import traceback
from typing import Any, Callable

from src.er2data.er2query import normalize_er_input_payload, read_json
from src.er2data.er_selection import (
    DEFAULT_PROMPT_DIR as DEFAULT_ER_SELECTION_PROMPT_DIR,
    DEFAULT_PROMPT_TEMPLATE_NAME as DEFAULT_ER_SELECTION_PROMPT_TEMPLATE_NAME,
    DEFAULT_SAMPLE_ROW_LIMIT as DEFAULT_ER_SELECTION_SAMPLE_ROW_LIMIT,
    DEFAULT_SAMPLE_VALUE_MAX_CHARS as DEFAULT_ER_SELECTION_SAMPLE_VALUE_MAX_CHARS,
    build_entity_candidates,
    build_relation_candidates,
    build_schema_snapshot,
    merge_er_models,
    select_entities_with_llm,
)
from src.er2data.sql_candidates import (
    DEFAULT_ER2QUERY_TEMPLATE_NAME,
    DEFAULT_PROMPT_DIR as DEFAULT_ER2DATA_PROMPT_DIR,
    ER2DataSQLCandidateRunner,
)
from src.llm.reasoning import REASONING_MODE_MAP
from src.nl2er.input import NL2ERInput, read_input_payloads
from src.nl2er.nl2er_hypothesis import (
    DEFAULT_PROMPT_DIR as DEFAULT_NL2ER_PROMPT_DIR,
    PROMPT_TEMPLATE_NAME as DEFAULT_NL2ER_PROMPT_TEMPLATE_NAME,
    QUESTION_RESOLVER_TEMPLATE_NAME as DEFAULT_QUESTION_RESOLVER_TEMPLATE_NAME,
    NL2ERHypothesisRunner,
)
from src.nl2er.nl2er_hypothesis_diff import (
    DEFAULT_PROMPT_DIR as DEFAULT_NL2ER_DIFF_PROMPT_DIR,
    NL2ERHypothesisDiffConstructor,
)
from src.nl2sql.defaults import (
    DEFAULT_ENGINE_SCRIPT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SPIDER2_ROOT,
)
from src.utils.run_log import build_timestamp

DEFAULT_INPUT_PATH = Path("data/input.json")
DEFAULT_METADATA_ROOT = Path("metadata")
DEFAULT_LOG_ROOT = Path("log/knowdata_pipeline")

NL2ER_FILENAME = "nl2er.json"
NL2ER_DIFF_FILENAME = "nl2er_diff.json"
SQL_CANDIDATES_FILENAME = "sql_candidates.json"
SCHEMA_LINKING_FILENAME = "schema_linking.json"
ER_SELECTION_FILENAME = "er_selection.json"

SAFE_PATH_RE = re.compile(r"[^A-Za-z0-9_.-]+")


PipelineInput = NL2ERInput


@dataclass(slots=True)
class PipelineCase:
    input_payload: PipelineInput
    case_dir: Path
    relative_case_dir: Path

    @property
    def nl2er_path(self) -> Path:
        return self.case_dir / NL2ER_FILENAME

    @property
    def nl2er_diff_path(self) -> Path:
        return self.case_dir / NL2ER_DIFF_FILENAME

    @property
    def sql_candidates_path(self) -> Path:
        return self.case_dir / SQL_CANDIDATES_FILENAME

    @property
    def schema_linking_path(self) -> Path:
        return self.case_dir / SCHEMA_LINKING_FILENAME

    @property
    def er_selection_path(self) -> Path:
        return self.case_dir / ER_SELECTION_FILENAME


def resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        return (Path.cwd() / resolved).resolve()
    return resolved.resolve()


def write_json(path: str | Path, payload: Any) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def write_text(path: str | Path, content: str) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(content, encoding="utf-8")


def safe_path_name(value: str) -> str:
    cleaned = re.sub(r"_+", "_", SAFE_PATH_RE.sub("_", str(value or ""))).strip("._-")
    return cleaned or "case"


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
    raise argparse.ArgumentTypeError(f"Expected `true` or `false`, got `{value}`.")


def read_pipeline_inputs(path: str | Path) -> list[PipelineInput]:
    return read_input_payloads(
        resolve_path(path),
        require_question_id=True,
        allow_question_id_from_db_id=True,
    )


def normalize_question_ids(values: list[str] | None) -> list[str]:
    if not values:
        return []
    output: list[str] = []
    seen: set[str] = set()
    for value in values:
        for item in value.split(","):
            question_id = item.strip()
            if not question_id or question_id in seen:
                continue
            output.append(question_id)
            seen.add(question_id)
    return output


def select_inputs(
    all_inputs: list[PipelineInput],
    requested_question_ids: list[str],
) -> list[PipelineInput]:
    if not requested_question_ids:
        return all_inputs
    by_question_id = {item.question_id: item for item in all_inputs}
    missing = [item for item in requested_question_ids if item not in by_question_id]
    if missing:
        raise ValueError(
            "The following question_id values were not found in the input file: "
            + ", ".join(missing)
        )
    return [by_question_id[item] for item in requested_question_ids]


def build_cases(
    inputs: list[PipelineInput],
    *,
    metadata_dir: Path,
    timestamp: str,
) -> list[PipelineCase]:
    cases: list[PipelineCase] = []
    for item in inputs:
        case_name = f"{safe_path_name(item.question_id)}_{timestamp}"
        cases.append(
            PipelineCase(
                input_payload=item,
                case_dir=metadata_dir / case_name,
                relative_case_dir=Path(case_name),
            )
        )
    return cases


def enrich_nl2er_payload(payload: dict[str, Any], input_payload: PipelineInput) -> dict[str, Any]:
    enriched = dict(payload)
    enriched.setdefault("question_id", input_payload.question_id)
    enriched.setdefault("db_id", input_payload.db_id)
    enriched.setdefault("user_intent", input_payload.user_intent)
    if input_payload.db_hint:
        enriched.setdefault("db_hint", input_payload.db_hint)
    if input_payload.external_knowledge:
        enriched.setdefault("external_knowledge", input_payload.external_knowledge)
    return enriched


def run_nl2er_case(
    case: PipelineCase,
    *,
    args: argparse.Namespace,
    log_root: Path,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    log_dir = log_root / case.relative_case_dir / "nl2er"
    runner = NL2ERHypothesisRunner(
        prompt_dir=args.nl2er_prompt_dir,
        log_dir=log_dir,
        input_payload=case.input_payload,
        model_config=args.nl2er_model_config,
        reasoning_mode=args.reasoning_mode,
        prompt_template_name=args.nl2er_prompt_template_name,
        question_resolver_template_name=args.question_resolver_template_name,
        include_question_ambiguity_in_er_extract=getattr(
            args,
            "include_question_ambiguity_in_er_extract",
            False,
        ),
    )
    payload = runner.run()
    result = enrich_nl2er_payload(dict(payload["result"]), case.input_payload)
    write_json(case.nl2er_path, result)
    return {
        "ok": True,
        "question_id": case.input_payload.question_id,
        "case_dir": str(case.case_dir),
        "output_path": str(case.nl2er_path),
        "log_dir": str(log_dir),
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }


def run_nl2er_diff_case(
    case: PipelineCase,
    *,
    args: argparse.Namespace,
    log_root: Path,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    log_dir = log_root / case.relative_case_dir / "nl2er_diff"
    constructor = NL2ERHypothesisDiffConstructor(
        prompt_dir=args.nl2er_diff_prompt_dir,
        log_dir=log_dir,
        model_config=args.nl2er_model_config,
        reasoning_mode=args.reasoning_mode,
        max_retry=args.diff_max_retry,
        dry_run=bool(args.diff_dry_run),
    )
    payload = constructor.run_case(
        input_path=case.nl2er_path,
        output_path=case.nl2er_diff_path,
        idea_count=args.diff_idea_count,
        question_id=case.input_payload.question_id,
        db_id=case.input_payload.db_id,
        user_intent=case.input_payload.user_intent,
        db_hint=case.input_payload.db_hint,
        external_knowledge=case.input_payload.external_knowledge,
        strategy_focus=args.strategy_focus,
        include_resolve_process=bool(args.include_resolve_process),
        include_schema_linking=False,
    )
    return {
        "ok": bool(payload.get("ok")),
        "status": str(payload.get("status") or payload.get("construction_status") or ""),
        "question_id": case.input_payload.question_id,
        "case_dir": str(case.case_dir),
        "input_path": str(case.nl2er_path),
        "output_path": str(case.nl2er_diff_path),
        "log_dir": str(log_dir),
        "idea_count": len(payload.get("diff_ideas") or []),
        "errors": payload.get("errors") or [],
        "warnings": payload.get("warnings") or [],
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }


def run_er2data_case(
    case: PipelineCase,
    *,
    args: argparse.Namespace,
    log_root: Path,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    log_dir = log_root / case.relative_case_dir / "er2data"
    resolved_enable_er2query_db_hint = (
        args.enable_nl2sql_db_hint
        if args.enable_er2query_db_hint is None
        else args.enable_er2query_db_hint
    )
    runner = ER2DataSQLCandidateRunner(
        prompt_dir=args.er2data_prompt_dir,
        log_dir=log_dir,
        er2query_template_name=args.er2query_template_name,
        question_model_config=args.er2data_question_model_config,
        candidate_model_config=args.er2data_candidate_model_config,
        reasoning_mode=args.reasoning_mode,
        include_desc_in_er2query=not args.exclude_desc_in_er2query,
        include_conditions_in_er2query=args.include_conditions_in_er2query,
        log_layout="compact",
        write_wrapper_logs=False,
        create_schema_linking_log_dirs=False,
    )
    payload = runner.run_case(
        input_path=case.nl2er_path,
        output_path=case.sql_candidates_path,
        db_id=case.input_payload.db_id,
        engine_provider=args.engine_provider,
        spider2_root=args.spider2_root,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
        nl2sql_engine_script=args.nl2sql_engine_script,
        max_question_concurrency=args.max_question_concurrency,
        max_candidate_workers=args.max_candidate_workers,
        candidate_timeout_seconds=args.candidate_timeout_seconds,
        candidate_temperature=args.candidate_temperature,
        schema_link_temperature=args.schema_link_temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.er2data_sample_row_limit,
        sample_value_max_chars=args.er2data_sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        num_votes=args.num_votes,
        reforce_max_workers=args.reforce_max_workers,
        max_iter=args.max_iter,
        generation_model=args.generation_model,
        column_exploration_model=args.column_exploration_model,
        vote_model=args.vote_model,
        db_hint=case.input_payload.db_hint,
        enable_nl2sql_db_hint=args.enable_nl2sql_db_hint,
        enable_er2query_db_hint=resolved_enable_er2query_db_hint,
        external_knowledge=case.input_payload.external_knowledge,
        include_er_diff=True,
        er_diff_filename=NL2ER_DIFF_FILENAME,
    )
    return {
        "ok": bool(payload.get("ok")),
        "question_id": case.input_payload.question_id,
        "case_dir": str(case.case_dir),
        "input_path": str(case.nl2er_path),
        "diff_input_path": str(case.nl2er_diff_path),
        "output_path": str(case.sql_candidates_path),
        "schema_linking_output_path": str(case.schema_linking_path),
        "log_dir": str(log_dir),
        "summary": payload.get("summary", {}),
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }


def _extract_fullnames(items: Any) -> list[str]:
    if not isinstance(items, list):
        return []
    output: list[str] = []
    for item in items:
        if isinstance(item, dict):
            value = str(
                item.get("fullname")
                or item.get("full_name")
                or item.get("table_fullname")
                or item.get("column_fullname")
                or item.get("name")
                or ""
            ).strip()
        else:
            value = str(item or "").strip()
        if value:
            output.append(value)
    return output


def collect_schema_linking_names(payload: Any) -> tuple[list[str], list[str]]:
    table_names: set[str] = set()
    column_names: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            table_names.update(_extract_fullnames(value.get("linked_tables")))
            column_names.update(_extract_fullnames(value.get("linked_columns")))
            for child in value.values():
                visit(child)
            return
        if isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return sorted(table_names), sorted(column_names)


def build_er_selection_output(
    *,
    case: PipelineCase,
    schema_snapshot: dict[str, Any],
    entity_candidates: list[dict[str, Any]],
    relation_candidates: list[dict[str, Any]],
    selection_result: dict[str, Any],
) -> dict[str, Any]:
    selection = selection_result.get("selection") or {
        "entities": [],
        "selected_entities": [],
    }
    selected_entities = list(
        selection.get("entities") or selection.get("selected_entities") or []
    )
    selected_relations = list(
        selection.get("relations") or selection.get("selected_relations") or []
    )
    return {
        "ok": bool(selection_result.get("ok")),
        "error": str(selection_result.get("error") or ""),
        "question_id": case.input_payload.question_id,
        "db_id": case.input_payload.db_id,
        "question": case.input_payload.user_intent,
        "model_config": str(selection_result.get("model_config") or ""),
        "selection": selection,
        "summary": {
            "entity_candidate_count": len(entity_candidates),
            "selected_entity_count": len(selected_entities),
            "relation_candidate_count": len(relation_candidates),
            "selected_relation_count": len(selected_relations),
            "snapshot_table_count": int(schema_snapshot.get("table_count") or 0),
        },
    }


def run_er_selection_case(
    case: PipelineCase,
    *,
    args: argparse.Namespace,
    log_root: Path,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    log_dir = log_root / case.relative_case_dir / "er_selection"
    log_dir.mkdir(parents=True, exist_ok=True)
    er_model = normalize_er_input_payload(
        read_json(case.nl2er_path),
        input_path=case.nl2er_path,
        merge_connections_into_relationships=False,
    )
    selection_er_model = er_model
    if case.nl2er_diff_path.exists():
        diff_er_model = normalize_er_input_payload(
            read_json(case.nl2er_diff_path),
            input_path=case.nl2er_diff_path,
            merge_connections_into_relationships=False,
        )
        selection_er_model = merge_er_models(er_model, diff_er_model)
    entity_candidates = build_entity_candidates(selection_er_model)
    if not entity_candidates:
        raise ValueError(f"No entity candidates were found in {case.nl2er_path}.")
    relation_candidates = (
        build_relation_candidates(selection_er_model)
        if args.include_relation_candidates
        else []
    )

    schema_linking_payload = read_json(case.schema_linking_path)
    linked_tables, linked_columns = collect_schema_linking_names(schema_linking_payload)
    schema_snapshot = build_schema_snapshot(
        db_id=case.input_payload.db_id,
        linked_tables=linked_tables,
        linked_columns=linked_columns,
        spider2_root=args.spider2_root,
        sample_row_limit=args.er_selection_sample_row_limit,
        sample_value_max_chars=args.er_selection_sample_value_max_chars,
    )
    selection_result = select_entities_with_llm(
        question=case.input_payload.user_intent,
        external_knowledge=case.input_payload.external_knowledge,
        schema_snapshot=schema_snapshot,
        entity_candidates=entity_candidates,
        relation_candidates=relation_candidates,
        include_relation_candidates=args.include_relation_candidates,
        model_config_name=args.er_selection_model_config,
        reasoning_mode=args.reasoning_mode,
        prompt_dir=args.er_selection_prompt_dir,
        prompt_template_name=args.er_selection_prompt_template_name,
    )
    write_text(log_dir / "prompt.md", str(selection_result.get("prompt") or ""))
    write_text(log_dir / "raw_response.md", str(selection_result.get("raw_response") or ""))
    write_json(log_dir / "schema_snapshot.json", schema_snapshot)

    output_payload = build_er_selection_output(
        case=case,
        schema_snapshot=schema_snapshot,
        entity_candidates=entity_candidates,
        relation_candidates=relation_candidates,
        selection_result=selection_result,
    )
    write_json(case.er_selection_path, output_payload)
    return {
        "ok": bool(output_payload.get("ok")),
        "question_id": case.input_payload.question_id,
        "case_dir": str(case.case_dir),
        "input_path": str(case.nl2er_path),
        "schema_linking_path": str(case.schema_linking_path),
        "output_path": str(case.er_selection_path),
        "log_dir": str(log_dir),
        "summary": output_payload.get("summary", {}),
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }


def run_case_safe(
    case: PipelineCase,
    *,
    stage: str,
    runner: Callable[[PipelineCase], dict[str, Any]],
    log_root: Path,
) -> dict[str, Any]:
    try:
        return runner(case)
    except Exception as exc:  # noqa: BLE001
        failure_payload = {
            "ok": False,
            "stage": stage,
            "question_id": case.input_payload.question_id,
            "case_dir": str(case.case_dir),
            "error": str(exc),
            "traceback": traceback.format_exc(),
        }
        write_json(log_root / case.relative_case_dir / stage / "error.json", failure_payload)
        return failure_payload


def run_stage(
    *,
    stage: str,
    cases: list[PipelineCase],
    max_workers: int,
    log_root: Path,
    runner: Callable[[PipelineCase], dict[str, Any]],
    active_predicate: Callable[[PipelineCase], bool] | None = None,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    active_cases = [
        case for case in cases if active_predicate is None or active_predicate(case)
    ]
    print(f"[KNOWDATA_PIPELINE] stage={stage} cases={len(active_cases)}")
    summaries: list[dict[str, Any]] = []
    if active_cases:
        worker_count = max(1, min(max_workers, len(active_cases)))
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    run_case_safe,
                    case,
                    stage=stage,
                    runner=runner,
                    log_root=log_root,
                ): case
                for case in active_cases
            }
            for future in as_completed(future_map):
                summary = future.result()
                summaries.append(summary)
                print(
                    "[KNOWDATA_PIPELINE] progress "
                    f"stage={stage} completed={len(summaries)}/{len(active_cases)} "
                    f"question_id={summary.get('question_id', '')} "
                    f"ok={summary.get('ok', False)}"
                )
    ok_count = sum(1 for item in summaries if item.get("ok"))
    stage_summary = {
        "stage": stage,
        "case_count": len(active_cases),
        "ok_cases": ok_count,
        "failed_cases": len(active_cases) - ok_count,
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "runs": summaries,
    }
    write_json(log_root / f"{stage}_summary.json", stage_summary)
    print(
        "[KNOWDATA_PIPELINE] stage_completed "
        f"stage={stage} cases={stage_summary['case_count']} "
        f"ok={stage_summary['ok_cases']} failed={stage_summary['failed_cases']} "
        f"elapsed={stage_summary['elapsed_seconds']}s"
    )
    return stage_summary


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run Knowdata NL2ER -> ER2Data SQL candidates/schema linking -> "
            "ER selection using functional modules."
        )
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run. Supports multiple values or comma-separated values.",
    )
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--timestamp", default=None)
    parser.add_argument("--max-stage-workers", type=positive_int, default=1)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )

    parser.add_argument("--nl2er-prompt-dir", type=Path, default=DEFAULT_NL2ER_PROMPT_DIR)
    parser.add_argument("--nl2er-prompt-template-name", default=DEFAULT_NL2ER_PROMPT_TEMPLATE_NAME)
    parser.add_argument(
        "--question-resolver-template-name",
        default=DEFAULT_QUESTION_RESOLVER_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--include-question-ambiguity-in-er-extract",
        action="store_true",
        help="Pass question_resolve ambiguity fields into the NL2ER ER extraction prompt.",
    )
    parser.add_argument("--nl2er-model-config", default="deepseek_v4_flash")
    parser.add_argument("--nl2er-diff-prompt-dir", type=Path, default=DEFAULT_NL2ER_DIFF_PROMPT_DIR)
    parser.add_argument("--diff-idea-count", type=positive_int, default=4)
    parser.add_argument("--diff-max-retry", type=int, default=1)
    parser.add_argument("--diff-dry-run", action="store_true")
    parser.add_argument("--include-resolve-process", action="store_true")
    parser.add_argument("--strategy-focus", default="")

    parser.add_argument("--er2data-prompt-dir", type=Path, default=DEFAULT_ER2DATA_PROMPT_DIR)
    parser.add_argument("--er2query-template-name", default=DEFAULT_ER2QUERY_TEMPLATE_NAME)
    parser.add_argument("--er2data-question-model-config", default="deepseek_chat")
    parser.add_argument("--er2data-candidate-model-config", default="deepseek_chat")
    parser.add_argument("--engine-provider", default="reforce_gen_sl_m1")
    parser.add_argument("--spider2-root", type=Path, default=DEFAULT_SPIDER2_ROOT)
    parser.add_argument("--reforce-root", type=Path, default=DEFAULT_REFORCE_ROOT)
    parser.add_argument("--engine-script", type=Path, default=DEFAULT_ENGINE_SCRIPT)
    parser.add_argument("--nl2sql-engine-script", type=Path, default=None)
    parser.add_argument("--max-question-concurrency", type=int, default=None)
    parser.add_argument("--max-candidate-workers", type=positive_int, default=24)
    parser.add_argument("--candidate-timeout-seconds", type=float, default=900.0)
    parser.add_argument("--candidate-temperature", type=float, default=0.7)
    parser.add_argument("--schema-link-temperature", type=float, default=0.0)
    parser.add_argument("--shortlist-trigger", type=int, default=18)
    parser.add_argument("--max-shortlist-tables", type=int, default=24)
    parser.add_argument("--er2data-sample-row-limit", type=int, default=2)
    parser.add_argument("--er2data-sample-value-max-chars", type=int, default=300)
    parser.add_argument("--similar-tables-hint-limit", type=int, default=12)
    parser.add_argument("--num-votes", type=positive_int, default=2)
    parser.add_argument("--reforce-max-workers", type=positive_int, default=24)
    parser.add_argument("--max-iter", type=positive_int, default=5)
    parser.add_argument("--generation-model", default=None)
    parser.add_argument("--column-exploration-model", default=None)
    parser.add_argument("--vote-model", default=None)
    parser.add_argument(
        "--enable-nl2sql-db-hint",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--enable-er2query-db-hint",
        type=parse_cli_bool,
        default=False,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--exclude-desc-in-er2query",
        type=parse_cli_bool,
        nargs="?",
        const=True,
        default=False,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--include-conditions-in-er2query",
        type=parse_cli_bool,
        nargs="?",
        const=True,
        default=True,
        metavar="{true,false}",
    )

    parser.add_argument("--er-selection-prompt-dir", type=Path, default=DEFAULT_ER_SELECTION_PROMPT_DIR)
    parser.add_argument(
        "--er-selection-prompt-template-name",
        default=DEFAULT_ER_SELECTION_PROMPT_TEMPLATE_NAME,
    )
    parser.add_argument("--er-selection-model-config", default="deepseek_v4_flash")
    parser.add_argument(
        "--include-relation-candidates",
        type=parse_cli_bool,
        default=True,
        metavar="{true,false}",
    )
    parser.add_argument(
        "--er-selection-sample-row-limit",
        type=int,
        default=DEFAULT_ER_SELECTION_SAMPLE_ROW_LIMIT,
    )
    parser.add_argument(
        "--er-selection-sample-value-max-chars",
        type=int,
        default=DEFAULT_ER_SELECTION_SAMPLE_VALUE_MAX_CHARS,
    )

    return parser


def run_pipeline(args: argparse.Namespace) -> dict[str, Any]:
    timestamp = str(args.timestamp or build_timestamp())
    selected_inputs = select_inputs(
        read_pipeline_inputs(args.input_path),
        normalize_question_ids(args.question_id),
    )
    metadata_dir = resolve_path(args.metadata_root) / timestamp
    log_root = resolve_path(args.log_root) / timestamp
    metadata_dir.mkdir(parents=True, exist_ok=True)
    log_root.mkdir(parents=True, exist_ok=True)

    cases = build_cases(selected_inputs, metadata_dir=metadata_dir, timestamp=timestamp)
    print(f"[KNOWDATA_PIPELINE] timestamp={timestamp}")
    print(f"[KNOWDATA_PIPELINE] input_path={resolve_path(args.input_path)}")
    print(f"[KNOWDATA_PIPELINE] metadata_dir={metadata_dir}")
    print(f"[KNOWDATA_PIPELINE] log_root={log_root}")
    print(f"[KNOWDATA_PIPELINE] selected_cases={len(cases)}")

    nl2er_summary = run_stage(
        stage="nl2er",
        cases=cases,
        max_workers=args.max_stage_workers,
        log_root=log_root,
        runner=lambda case: run_nl2er_case(case, args=args, log_root=log_root),
    )
    nl2er_diff_summary = run_stage(
        stage="nl2er_diff",
        cases=cases,
        max_workers=args.max_stage_workers,
        log_root=log_root,
        active_predicate=lambda case: case.nl2er_path.exists(),
        runner=lambda case: run_nl2er_diff_case(case, args=args, log_root=log_root),
    )
    er2data_summary = run_stage(
        stage="er2data",
        cases=cases,
        max_workers=args.max_stage_workers,
        log_root=log_root,
        active_predicate=lambda case: case.nl2er_path.exists() and case.nl2er_diff_path.exists(),
        runner=lambda case: run_er2data_case(case, args=args, log_root=log_root),
    )
    er_selection_summary = run_stage(
        stage="er_selection",
        cases=cases,
        max_workers=args.max_stage_workers,
        log_root=log_root,
        active_predicate=lambda case: case.nl2er_path.exists() and case.schema_linking_path.exists(),
        runner=lambda case: run_er_selection_case(case, args=args, log_root=log_root),
    )

    main_stage_summaries = [nl2er_summary, nl2er_diff_summary, er2data_summary, er_selection_summary]
    status = (
        "succeeded"
        if all(item["case_count"] == item["ok_cases"] for item in main_stage_summaries)
        else "failed"
    )
    summary = {
        "timestamp": timestamp,
        "status": status,
        "input_path": str(resolve_path(args.input_path)),
        "metadata_dir": str(metadata_dir),
        "log_root": str(log_root),
        "selected_question_ids": [item.input_payload.question_id for item in cases],
        "outputs": {
            "nl2er": NL2ER_FILENAME,
            "nl2er_diff": NL2ER_DIFF_FILENAME,
            "sql_candidates": SQL_CANDIDATES_FILENAME,
            "schema_linking": SCHEMA_LINKING_FILENAME,
            "er_selection": ER_SELECTION_FILENAME,
        },
        "stages": {
            "nl2er": nl2er_summary,
            "nl2er_diff": nl2er_diff_summary,
            "er2data": er2data_summary,
            "er_selection": er_selection_summary,
        },
    }
    write_json(log_root / "knowdata_pipeline_summary.json", summary)
    print(
        "[KNOWDATA_PIPELINE] completed "
        f"status={status} metadata_dir={metadata_dir} summary={log_root / 'knowdata_pipeline_summary.json'}"
    )
    return summary


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        summary = run_pipeline(args)
    except KeyboardInterrupt:
        print("[KNOWDATA_PIPELINE] interrupted by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[KNOWDATA_PIPELINE] failed: {exc}")
        print(traceback.format_exc())
        return 1
    return 0 if summary.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
