from __future__ import annotations

import argparse
from pathlib import Path
import traceback
from typing import Any

from src.llm.reasoning import REASONING_MODE_MAP
from src.nl2er.nl2er_hypothesis import (
    DEFAULT_PROMPT_DIR as DEFAULT_NL2ER_PROMPT_DIR,
    PROMPT_TEMPLATE_NAME as DEFAULT_NL2ER_PROMPT_TEMPLATE_NAME,
    QUESTION_RESOLVER_TEMPLATE_NAME as DEFAULT_QUESTION_RESOLVER_TEMPLATE_NAME,
)
from src.nl2er.nl2er_hypothesis_diff import (
    DEFAULT_PROMPT_DIR as DEFAULT_NL2ER_DIFF_PROMPT_DIR,
)
from src.run.knowdata_pipeline import (
    DEFAULT_INPUT_PATH,
    DEFAULT_METADATA_ROOT,
    NL2ER_DIFF_FILENAME,
    NL2ER_FILENAME,
    build_cases,
    normalize_question_ids,
    positive_int,
    read_pipeline_inputs,
    resolve_path,
    run_nl2er_diff_case,
    run_nl2er_case,
    run_stage,
    select_inputs,
    write_json,
)
from src.utils.run_log import build_timestamp


DEFAULT_LOG_ROOT = Path("log/knowdata_nl2er_stage")
SUMMARY_FILENAME = "knowdata_nl2er_stage_summary.json"


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run only the Knowdata NL2ER stage and write nl2er.json metadata."
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
    return parser


def run_nl2er_stage(args: argparse.Namespace) -> dict[str, Any]:
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
    print(f"[KNOWDATA_NL2ER_STAGE] timestamp={timestamp}")
    print(f"[KNOWDATA_NL2ER_STAGE] input_path={resolve_path(args.input_path)}")
    print(f"[KNOWDATA_NL2ER_STAGE] metadata_dir={metadata_dir}")
    print(f"[KNOWDATA_NL2ER_STAGE] log_root={log_root}")
    print(f"[KNOWDATA_NL2ER_STAGE] selected_cases={len(cases)}")

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
    stage_summaries = [nl2er_summary, nl2er_diff_summary]
    status = (
        "succeeded"
        if all(item["case_count"] == item["ok_cases"] for item in stage_summaries)
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
        },
        "stages": {
            "nl2er": nl2er_summary,
            "nl2er_diff": nl2er_diff_summary,
        },
    }
    summary_path = log_root / SUMMARY_FILENAME
    write_json(summary_path, summary)
    print(
        "[KNOWDATA_NL2ER_STAGE] completed "
        f"status={status} metadata_dir={metadata_dir} summary={summary_path}"
    )
    return summary


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    try:
        summary = run_nl2er_stage(args)
    except KeyboardInterrupt:
        print("[KNOWDATA_NL2ER_STAGE] interrupted by user")
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"[KNOWDATA_NL2ER_STAGE] failed: {exc}")
        print(traceback.format_exc())
        return 1
    return 0 if summary.get("status") == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
