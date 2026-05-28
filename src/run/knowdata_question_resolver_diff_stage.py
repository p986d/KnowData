from __future__ import annotations

import argparse
from pathlib import Path
import time
import traceback
from typing import Any

from src.llm.reasoning import REASONING_MODE_MAP
from src.nl2er.nl2er_hypothesis import (
    DEFAULT_PROMPT_DIR as DEFAULT_NL2ER_PROMPT_DIR,
    QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME,
    QUESTION_RESOLVER_TEMPLATE_NAME,
    QuestionResolverDiff,
)
from src.run.knowdata_pipeline import (
    DEFAULT_INPUT_PATH,
    DEFAULT_METADATA_ROOT,
    PipelineCase,
    build_cases,
    normalize_question_ids,
    positive_int,
    read_pipeline_inputs,
    resolve_path,
    run_stage,
    select_inputs,
    write_json,
)
from src.utils.run_log import build_timestamp


DEFAULT_LOG_ROOT = Path("log/knowdata_question_resolver_diff_stage")
SUBPROBLEM_ANALYSIS_FILENAME = "subproblem_analysis.json"
QUESTION_RESOLVER_DIFF_FILENAME = "question_resolver_diff.json"
SUMMARY_FILENAME = "knowdata_question_resolver_diff_stage_summary.json"


def run_question_resolver_diff_case(
    case: PipelineCase,
    *,
    args: argparse.Namespace,
    log_root: Path,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    log_dir = log_root / case.relative_case_dir / "question_resolver_diff"
    log_dir.mkdir(parents=True, exist_ok=True)

    diff_runner = QuestionResolverDiff(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=case.input_payload,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        question_resolver_template_name=args.question_resolver_template_name,
        construct_template_name=args.question_resolver_diff_construct_template_name,
    )
    diff_payload = diff_runner.run()
    subproblem_analysis = dict(diff_payload["subproblem_analysis"])
    subproblem_output_path = case.case_dir / SUBPROBLEM_ANALYSIS_FILENAME
    write_json(subproblem_output_path, subproblem_analysis)

    output_payload = {
        "ok": True,
        "question_id": case.input_payload.question_id,
        "db_id": case.input_payload.db_id,
        "subproblem_analysis": subproblem_analysis,
        "resolve_diff": diff_payload["resolve_diff"],
        "summary": {
            "subproblem_count": len(subproblem_analysis.get("resolve_process") or []),
            "question_patch_count": len(
                (diff_payload["resolve_diff"] or {}).get("question_patches") or []
            ),
        },
    }
    output_path = case.case_dir / QUESTION_RESOLVER_DIFF_FILENAME
    write_json(output_path, output_payload)

    return {
        "ok": True,
        "question_id": case.input_payload.question_id,
        "case_dir": str(case.case_dir),
        "subproblem_output_path": str(subproblem_output_path),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "summary": output_payload["summary"],
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
    }


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run question resolve plus question-resolve diff construction as one "
            "Knowdata experiment stage."
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
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_NL2ER_PROMPT_DIR)
    parser.add_argument("--model-config", default="deepseek_v4_flash")
    parser.add_argument(
        "--question-resolver-template-name",
        default=QUESTION_RESOLVER_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--question-resolver-diff-construct-template-name",
        default=QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME,
    )
    return parser


def run_question_resolver_diff_stage(args: argparse.Namespace) -> dict[str, Any]:
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
    print(f"[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] timestamp={timestamp}")
    print(f"[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] input_path={resolve_path(args.input_path)}")
    print(f"[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] metadata_dir={metadata_dir}")
    print(f"[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] log_root={log_root}")
    print(f"[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] selected_cases={len(cases)}")

    stage_summary = run_stage(
        stage="question_resolver_diff",
        cases=cases,
        max_workers=args.max_stage_workers,
        log_root=log_root,
        runner=lambda case: run_question_resolver_diff_case(
            case,
            args=args,
            log_root=log_root,
        ),
    )
    status = (
        "succeeded"
        if stage_summary["case_count"] == stage_summary["ok_cases"]
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
            "subproblem_analysis": SUBPROBLEM_ANALYSIS_FILENAME,
            "question_resolver_diff": QUESTION_RESOLVER_DIFF_FILENAME,
        },
        "stages": {
            "question_resolver_diff": stage_summary,
        },
    }
    summary_path = log_root / SUMMARY_FILENAME
    write_json(summary_path, summary)
    print(
        "[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] completed "
        f"status={status} metadata_dir={metadata_dir} summary={summary_path}"
    )
    return summary


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        summary = run_question_resolver_diff_stage(args)
    except KeyboardInterrupt:
        print("[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] interrupted by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[KNOWDATA_QUESTION_RESOLVER_DIFF_STAGE] failed: {exc}")
        print(traceback.format_exc())
        return 1
    return 0 if summary.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
