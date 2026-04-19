from __future__ import annotations

import argparse
import json
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.run.nl2er_2 import (
    DEFAULT_INPUT_PATH,
    DEFAULT_LOG_ROOT,
    DEFAULT_METADATA_ROOT,
    DEFAULT_OUTPUT_FILENAME,
    DEFAULT_PROMPT_DIR,
    ER_TEST_ST1_OUTPUT_FILENAME,
    ER_TEST_ST2_OUTPUT_FILENAME,
    ERSkeletonIntegrityError,
    NL2ER,
    NL2ERInput,
    build_er_test_st1_output_payload,
    build_er_test_st2_output_payload,
    build_output_payload,
    write_case_metadata,
)
from src.utils.run_log import build_timestamp, resolve_run_dir, write_json


@dataclass(slots=True)
class BatchInput:
    question_id: str
    user_intent: str
    db_id: str
    db_hint: str = ""
    external_knowledge: str = ""


def serialize_input_payload(input_payload: BatchInput) -> dict[str, str]:
    return {
        "question_id": input_payload.question_id,
        "user_intent": input_payload.user_intent,
        "db_id": input_payload.db_id,
        "db_hint": input_payload.db_hint,
        "external_knowledge": input_payload.external_knowledge,
    }


def _parse_single_input(
    payload: object,
    *,
    input_path: Path,
    index: int | None = None,
) -> BatchInput:
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

    location = f"{input_path}[{index}]" if index is not None else str(input_path)
    if not question_id:
        raise ValueError(f"`question_id` is required in {location}")
    if not user_intent:
        raise ValueError(f"`user_intent` is required in {location}")
    if not db_id:
        raise ValueError(f"`db_id` is required in {location}")
    if not isinstance(db_hint, str):
        raise ValueError(f"`db_hint` must be a string in {location}")
    if not isinstance(external_knowledge, str):
        raise ValueError(f"`external_knowledge` must be a string in {location}")

    return BatchInput(
        question_id=question_id,
        user_intent=user_intent,
        db_id=db_id,
        db_hint=db_hint,
        external_knowledge=external_knowledge,
    )


def read_inputs(path: str | Path) -> list[BatchInput]:
    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if isinstance(payload, dict):
        return [_parse_single_input(payload, input_path=input_path)]
    if not isinstance(payload, list):
        raise ValueError(f"Expected JSON object or JSON list at {input_path}, got {type(payload).__name__}")

    inputs: list[BatchInput] = []
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


def select_inputs(all_inputs: list[BatchInput], requested_question_ids: list[str]) -> list[BatchInput]:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run nl2er_2 for one or more questions from the input file."
    )
    parser.add_argument(
        "--mode",
        choices=["pipeline", "er_test_st1", "er_test_st2"],
        default="pipeline",
        help="`pipeline` runs the current stage1/stage2 ER workflow and writes `nl2er_output` using the current st2 final schema; `er_test_st1` runs only the stage1 object sketch prompt; `er_test_st2` runs stage1 object sketch extraction plus stage2 ER review.",
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument(
        "--question-id",
        nargs="+",
        default=None,
        help="Question IDs to run. Supports multiple values, or a comma-separated list in one argument.",
    )
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    parser.add_argument("--nl2er-log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--model-config", default=None)
    parser.add_argument("--max-workers", type=positive_int, default=1)
    return parser.parse_args()


def build_case_summary(
    *,
    input_payload: BatchInput,
    run_id: str,
    run_timestamp: str,
    metadata_dir: Path,
    log_dir: Path,
    output_path: Path,
    mode: str,
    status: str,
    elapsed_seconds: float | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    summary: dict[str, object] = {
        "run_id": run_id,
        "question_id": input_payload.question_id,
        "db_id": input_payload.db_id,
        "timestamp": run_timestamp,
        "mode": mode,
        "metadata_dir": str(metadata_dir),
        "log_dir": str(log_dir),
        "output_path": str(output_path),
        "status": status,
    }
    if elapsed_seconds is not None:
        summary["elapsed_seconds"] = elapsed_seconds
    if error_message:
        summary["error_message"] = error_message
    return summary


def write_case_failure_log(
    *,
    log_dir: Path,
    input_payload: BatchInput,
    run_id: str,
    mode: str,
    error_type: str,
    error_message: str,
    traceback_text: str | None = None,
    integrity_report: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "run_id": run_id,
        "question_id": input_payload.question_id,
        "db_id": input_payload.db_id,
        "mode": mode,
        "error_type": error_type,
        "error_message": error_message,
    }
    if traceback_text:
        payload["traceback"] = traceback_text
    if integrity_report is not None:
        payload["integrity_report"] = integrity_report
    write_json(log_dir / "error.json", payload)


def _resolve_case_dir_path(run_root: Path, *, question_id: str, run_timestamp: str) -> Path:
    resolved_root = run_root.expanduser()
    if not resolved_root.is_absolute():
        resolved_root = (Path.cwd() / resolved_root).resolve()
    else:
        resolved_root = resolved_root.resolve()
    return resolved_root / f"{question_id}_{run_timestamp}"


def build_unhandled_case_summary(
    *,
    input_payload: BatchInput,
    run_timestamp: str,
    args: argparse.Namespace,
    error_message: str,
) -> dict[str, object]:
    metadata_dir = _resolve_case_dir_path(
        args.metadata_root,
        question_id=input_payload.question_id,
        run_timestamp=run_timestamp,
    )
    log_dir = _resolve_case_dir_path(
        args.nl2er_log_root,
        question_id=input_payload.question_id,
        run_timestamp=run_timestamp,
    )
    output_filename = (
        DEFAULT_OUTPUT_FILENAME
        if args.mode == "pipeline"
        else (
            ER_TEST_ST1_OUTPUT_FILENAME
            if args.mode == "er_test_st1"
            else ER_TEST_ST2_OUTPUT_FILENAME
        )
    )
    output_path = metadata_dir / output_filename
    run_id = f"{input_payload.question_id}_{run_timestamp}"
    return build_case_summary(
        input_payload=input_payload,
        run_id=run_id,
        run_timestamp=run_timestamp,
        metadata_dir=metadata_dir,
        log_dir=log_dir,
        output_path=output_path,
        mode=args.mode,
        status="failed",
        error_message=error_message,
    )


def emit_batch_progress(
    *,
    completed_count: int,
    total_count: int,
    run_summary: dict[str, object],
) -> None:
    print(
        "[NL2ER-2-ONLY] progress "
        f"completed={completed_count}/{total_count} "
        f"question_id={run_summary.get('question_id', '')} "
        f"status={run_summary.get('status', 'unknown')}"
    )


def run_single_question(
    *,
    input_payload: BatchInput,
    run_timestamp: str,
    args: argparse.Namespace,
) -> dict[str, object]:
    run_id = f"{input_payload.question_id}_{run_timestamp}"

    metadata_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=None,
        run_root=args.metadata_root,
        timestamp=run_timestamp,
    )
    log_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=None,
        run_root=args.nl2er_log_root,
        timestamp=run_timestamp,
    )
    output_filename = (
        DEFAULT_OUTPUT_FILENAME
        if args.mode == "pipeline"
        else (
            ER_TEST_ST1_OUTPUT_FILENAME
            if args.mode == "er_test_st1"
            else ER_TEST_ST2_OUTPUT_FILENAME
        )
    )
    output_path = metadata_dir / output_filename

    serialized_input = serialize_input_payload(input_payload)
    write_case_metadata(
        metadata_dir=metadata_dir,
        serialized_input=serialized_input,
        source_input_path=args.input_path,
        question_id=input_payload.question_id,
    )
    write_json(log_dir / "input.json", serialized_input)

    print(f"[NL2ER-2-ONLY] run_id={run_id}")
    print(f"[NL2ER-2-ONLY] question_id={input_payload.question_id}")
    print(f"[NL2ER-2-ONLY] db_id={input_payload.db_id}")
    print(f"[NL2ER-2-ONLY] mode={args.mode}")
    print(f"[NL2ER-2-ONLY] metadata_dir={metadata_dir}")
    print(f"[NL2ER-2-ONLY] log_dir={log_dir}")

    nl2er = NL2ER(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=NL2ERInput(
            question_id=input_payload.question_id,
            user_intent=input_payload.user_intent,
            db_id=input_payload.db_id,
            db_hint=input_payload.db_hint,
            external_knowledge=input_payload.external_knowledge,
        ),
        model_config=args.model_config,
    )

    if args.mode == "er_test_st1":
        try:
            payload = nl2er.run_er_test_st1()
        except Exception as exc:
            run_summary = build_case_summary(
                input_payload=input_payload,
                run_id=run_id,
                run_timestamp=run_timestamp,
                metadata_dir=metadata_dir,
                log_dir=log_dir,
                output_path=output_path,
                mode=args.mode,
                status="failed",
                error_message=str(exc),
            )
            write_case_failure_log(
                log_dir=log_dir,
                input_payload=input_payload,
                run_id=run_id,
                mode=args.mode,
                error_type=type(exc).__name__,
                error_message=str(exc),
                traceback_text=traceback.format_exc(),
            )
            print(f"[NL2ER-2-ONLY] run failed for {input_payload.question_id}: {exc}")
            return run_summary

        integrity_report = payload["integrity_report"]
        write_json(
            output_path,
            build_er_test_st1_output_payload(
                object_sketch=payload["result"],
                integrity_report=integrity_report,
            ),
        )

        status = "succeeded" if integrity_report.get("passed") else "integrity_failed"
        error_message = None if integrity_report.get("passed") else str(integrity_report.get("summary") or "")
        if status == "integrity_failed":
            write_case_failure_log(
                log_dir=log_dir,
                input_payload=input_payload,
                run_id=run_id,
                mode=args.mode,
                error_type="ERObjectSketchIntegrityError",
                error_message=error_message or "ER object sketch integrity check failed.",
                integrity_report=integrity_report,
            )
            print(
                f"[NL2ER-2-ONLY] object sketch integrity check failed for {input_payload.question_id}: "
                f"{error_message}"
            )
        else:
            print(f"[NL2ER-2-ONLY] output wrote to {output_path}")

        return build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            log_dir=log_dir,
            output_path=output_path,
            mode=args.mode,
            status=status,
            elapsed_seconds=float(payload["elapsed_seconds"]),
            error_message=error_message,
        )

    if args.mode == "er_test_st2":
        try:
            payload = nl2er.run_er_test_st2()
        except Exception as exc:
            run_summary = build_case_summary(
                input_payload=input_payload,
                run_id=run_id,
                run_timestamp=run_timestamp,
                metadata_dir=metadata_dir,
                log_dir=log_dir,
                output_path=output_path,
                mode=args.mode,
                status="failed",
                error_message=str(exc),
            )
            write_case_failure_log(
                log_dir=log_dir,
                input_payload=input_payload,
                run_id=run_id,
                mode=args.mode,
                error_type=type(exc).__name__,
                error_message=str(exc),
                traceback_text=traceback.format_exc(),
            )
            print(f"[NL2ER-2-ONLY] run failed for {input_payload.question_id}: {exc}")
            return run_summary

        integrity_report = payload["integrity_report"]
        write_json(
            output_path,
            build_er_test_st2_output_payload(
                stage1_object_sketch=payload["stage1_object_sketch"],
                stage1_integrity_report=payload["stage1_integrity_report"],
                semantic_review=payload["result"],
                integrity_report=integrity_report,
            ),
        )

        status = "succeeded" if integrity_report.get("passed") else "integrity_failed"
        error_message = None if integrity_report.get("passed") else str(integrity_report.get("summary") or "")
        if status == "integrity_failed":
            write_case_failure_log(
                log_dir=log_dir,
                input_payload=input_payload,
                run_id=run_id,
                mode=args.mode,
                error_type="ERSemanticReviewIntegrityError",
                error_message=error_message or "ER semantic review integrity check failed.",
                integrity_report=integrity_report,
            )
            print(
                f"[NL2ER-2-ONLY] semantic review integrity check failed for {input_payload.question_id}: "
                f"{error_message}"
            )
        else:
            print(f"[NL2ER-2-ONLY] output wrote to {output_path}")

        return build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            log_dir=log_dir,
            output_path=output_path,
            mode=args.mode,
            status=status,
            elapsed_seconds=float(payload["elapsed_seconds"]),
            error_message=error_message,
        )

    try:
        payload = nl2er.run()
    except ERSkeletonIntegrityError as exc:
        write_json(
            output_path,
            build_output_payload(
                input_payload=NL2ERInput(
                    question_id=input_payload.question_id,
                    user_intent=input_payload.user_intent,
                    db_id=input_payload.db_id,
                    db_hint=input_payload.db_hint,
                    external_knowledge=input_payload.external_knowledge,
                ),
                er_result={
                    **exc.result,
                    "integrity_report": exc.integrity_report,
                },
            ),
        )
        run_summary = build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            log_dir=log_dir,
            output_path=output_path,
            mode=args.mode,
            status="integrity_failed",
            error_message=str(exc),
        )
        write_case_failure_log(
            log_dir=log_dir,
            input_payload=input_payload,
            run_id=run_id,
            mode=args.mode,
            error_type=type(exc).__name__,
            error_message=str(exc),
            integrity_report=exc.integrity_report,
        )
        print(f"[NL2ER-2-ONLY] integrity check failed for {input_payload.question_id}: {exc}")
        print(f"[NL2ER-2-ONLY] partial output wrote to {output_path}")
        return run_summary
    except Exception as exc:
        run_summary = build_case_summary(
            input_payload=input_payload,
            run_id=run_id,
            run_timestamp=run_timestamp,
            metadata_dir=metadata_dir,
            log_dir=log_dir,
            output_path=output_path,
            mode=args.mode,
            status="failed",
            error_message=str(exc),
        )
        write_case_failure_log(
            log_dir=log_dir,
            input_payload=input_payload,
            run_id=run_id,
            mode=args.mode,
            error_type=type(exc).__name__,
            error_message=str(exc),
            traceback_text=traceback.format_exc(),
        )
        print(f"[NL2ER-2-ONLY] run failed for {input_payload.question_id}: {exc}")
        return run_summary

    write_json(
        output_path,
        build_output_payload(
            input_payload=NL2ERInput(
                question_id=input_payload.question_id,
                user_intent=input_payload.user_intent,
                db_id=input_payload.db_id,
                db_hint=input_payload.db_hint,
                external_knowledge=input_payload.external_knowledge,
            ),
            er_result={
                **payload["result"],
                "integrity_report": payload["integrity_report"],
            },
        ),
    )
    print(f"[NL2ER-2-ONLY] output wrote to {output_path}")

    return build_case_summary(
        input_payload=input_payload,
        run_id=run_id,
        run_timestamp=run_timestamp,
        metadata_dir=metadata_dir,
        log_dir=log_dir,
        output_path=output_path,
        mode=args.mode,
        status="succeeded",
        elapsed_seconds=float(payload["elapsed_seconds"]),
    )


def main() -> None:
    args = parse_args()

    all_inputs = read_inputs(args.input_path)
    requested_question_ids = normalize_question_ids(args.question_id)
    selected_inputs = select_inputs(all_inputs, requested_question_ids)

    print(f"[NL2ER-2-ONLY] selected {len(selected_inputs)} question(s) from {args.input_path}")
    print(f"[NL2ER-2-ONLY] mode={args.mode}")
    if requested_question_ids:
        print(f"[NL2ER-2-ONLY] requested question_id values: {', '.join(requested_question_ids)}")

    batch_timestamp = build_timestamp()
    total = len(selected_inputs)
    completed_count = 0
    should_run_parallel = args.max_workers > 1 and total > 1

    if should_run_parallel:
        worker_count = min(args.max_workers, total)
        print(f"[NL2ER-2-ONLY] running in parallel with max_workers={worker_count}")
        batch_summary_slots: list[dict[str, object] | None] = [None] * total
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {}
            for index, input_payload in enumerate(selected_inputs, start=1):
                print(f"[NL2ER-2-ONLY] starting question {index}/{total}: {input_payload.question_id}")
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
                    print(f"[NL2ER-2-ONLY] unhandled failure for {input_payload.question_id}: {exc}")
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
            raise RuntimeError("NL2ER_2 batch summary collection incomplete.")
        batch_summary = [item for item in batch_summary_slots if item is not None]
    else:
        batch_summary: list[dict[str, object]] = []
        for index, input_payload in enumerate(selected_inputs, start=1):
            print(f"[NL2ER-2-ONLY] starting question {index}/{total}: {input_payload.question_id}")
            try:
                run_summary = run_single_question(
                    input_payload=input_payload,
                    run_timestamp=batch_timestamp,
                    args=args,
                )
            except Exception as exc:
                print(f"[NL2ER-2-ONLY] unhandled failure for {input_payload.question_id}: {exc}")
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
    integrity_failed_count = sum(
        1 for item in batch_summary if item.get("status") == "integrity_failed"
    )
    failed_count = sum(1 for item in batch_summary if item.get("status") == "failed")
    batch_payload = {
        "timestamp": batch_timestamp,
        "mode": args.mode,
        "input_path": str(args.input_path),
        "selected_question_ids": [item.question_id for item in selected_inputs],
        "max_workers": min(args.max_workers, total) if total else args.max_workers,
        "runs": batch_summary,
    }
    print(
        f"[NL2ER-2-ONLY] completed {total} question(s): "
        f"succeeded={succeeded_count}, integrity_failed={integrity_failed_count}, failed={failed_count}"
    )
    print("[NL2ER-2-ONLY] batch summary follows")
    print(json.dumps(batch_payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
