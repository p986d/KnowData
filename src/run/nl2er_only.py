from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from src.run.nl2er import (
    DEFAULT_INPUT_PATH,
    DEFAULT_LOG_ROOT,
    DEFAULT_OUTPUT_FILENAME,
    DEFAULT_PROMPT_DIR,
    NL2ER,
    NL2ERInput,
    build_output_payload,
)
from src.utils.run_log import build_timestamp, resolve_run_dir, write_json


DEFAULT_METADATA_ROOT = Path("metadata")


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


def _parse_single_input(payload: object, *, input_path: Path, index: int | None = None) -> BatchInput:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run only the NL2ER stage for one or more questions from the input file."
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
    return parser.parse_args()


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
    output_path = metadata_dir / DEFAULT_OUTPUT_FILENAME

    write_json(metadata_dir / "input.json", serialize_input_payload(input_payload))
    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": input_payload.question_id,
            "db_id": input_payload.db_id,
            "timestamp": run_timestamp,
            "metadata_dir": str(metadata_dir),
            "log_dir": str(log_dir),
            "output_path": str(output_path),
        },
    )
    write_json(log_dir / "input.json", serialize_input_payload(input_payload))

    print(f"[NL2ER-ONLY] run_id={run_id}")
    print(f"[NL2ER-ONLY] question_id={input_payload.question_id}")
    print(f"[NL2ER-ONLY] db_id={input_payload.db_id}")
    print(f"[NL2ER-ONLY] metadata_dir={metadata_dir}")
    print(f"[NL2ER-ONLY] log_dir={log_dir}")

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
    payload = nl2er.run()
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
            er_result=payload["result"],
        ),
    )
    print(f"[NL2ER-ONLY] output wrote to {output_path}")

    run_summary = {
        "run_id": run_id,
        "question_id": input_payload.question_id,
        "db_id": input_payload.db_id,
        "timestamp": run_timestamp,
        "metadata_dir": str(metadata_dir),
        "log_dir": str(log_dir),
        "output_path": str(output_path),
        "elapsed_seconds": payload["elapsed_seconds"],
    }
    write_json(metadata_dir / "run_context.json", run_summary)
    return run_summary


def main() -> None:
    args = parse_args()

    all_inputs = read_inputs(args.input_path)
    requested_question_ids = normalize_question_ids(args.question_id)
    selected_inputs = select_inputs(all_inputs, requested_question_ids)

    print(f"[NL2ER-ONLY] selected {len(selected_inputs)} question(s) from {args.input_path}")
    if requested_question_ids:
        print(f"[NL2ER-ONLY] requested question_id values: {', '.join(requested_question_ids)}")

    batch_timestamp = build_timestamp()
    batch_summary: list[dict[str, object]] = []
    total = len(selected_inputs)
    for index, input_payload in enumerate(selected_inputs, start=1):
        print(f"[NL2ER-ONLY] starting question {index}/{total}: {input_payload.question_id}")
        batch_summary.append(
            run_single_question(
                input_payload=input_payload,
                run_timestamp=batch_timestamp,
                args=args,
            )
        )

    batch_summary_path = args.metadata_root / f"nl2er_batch_{batch_timestamp}.json"
    write_json(
        batch_summary_path,
        {
            "timestamp": batch_timestamp,
            "input_path": str(args.input_path),
            "selected_question_ids": [item.question_id for item in selected_inputs],
            "runs": batch_summary,
        },
    )
    print(f"[NL2ER-ONLY] batch summary wrote to {batch_summary_path}")


if __name__ == "__main__":
    main()
