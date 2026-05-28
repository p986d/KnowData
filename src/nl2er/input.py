from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any


DEFAULT_INPUT_PATH = Path("data/input.json")
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_METADATA_ROOT = Path("metadata")
GROUND_TRUTH_OUTPUT_FILENAME = "ground_truth.sql"


@dataclass(slots=True)
class NL2ERInput:
    question_id: str
    user_intent: str
    db_id: str
    db_hint: str = ""
    external_knowledge: str = ""


def _parse_input_payload_object(
    payload: object,
    *,
    input_path: Path,
    location: str | None = None,
    require_question_id: bool = False,
    allow_question_id_from_db_id: bool = False,
) -> NL2ERInput:
    resolved_location = location or str(input_path)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object at {resolved_location}, got {type(payload).__name__}"
        )

    question_id = str(payload.get("question_id") or payload.get("instance_id") or "").strip()
    db_id = str(payload.get("db_id") or "").strip()
    if not question_id and allow_question_id_from_db_id:
        question_id = db_id
    user_intent = str(payload.get("user_intent") or payload.get("instruction") or "").strip()
    db_hint = payload.get("db_hint", "")
    external_knowledge = payload.get("external_knowledge", "")

    if require_question_id and not question_id:
        raise ValueError(f"`question_id` is required in {resolved_location}")
    if not user_intent:
        raise ValueError(f"`user_intent` is required in {resolved_location}")
    if not db_id:
        raise ValueError(f"`db_id` is required in {resolved_location}")
    if not isinstance(db_hint, str):
        raise ValueError(f"`db_hint` must be a string in {resolved_location}")
    if not isinstance(external_knowledge, str):
        raise ValueError(f"`external_knowledge` must be a string in {resolved_location}")

    return NL2ERInput(
        question_id=question_id,
        user_intent=user_intent,
        db_id=db_id,
        db_hint=db_hint,
        external_knowledge=external_knowledge,
    )


def read_input_payload(
    path: str | Path,
    *,
    question_id: str | None = None,
) -> NL2ERInput:
    input_path = Path(path)
    payload = json.loads(input_path.read_text(encoding="utf-8"))

    if isinstance(payload, dict):
        return _parse_input_payload_object(payload, input_path=input_path)

    if not isinstance(payload, list):
        raise ValueError(
            f"Expected JSON object or JSON list at {input_path}, got {type(payload).__name__}"
        )

    if not payload:
        raise ValueError(f"Input list at {input_path} is empty.")

    normalized_question_id = str(question_id or "").strip()
    parsed_inputs = [
        _parse_input_payload_object(
            item,
            input_path=input_path,
            location=f"{input_path}[{index}]",
        )
        for index, item in enumerate(payload)
    ]

    if normalized_question_id:
        for parsed_input in parsed_inputs:
            if parsed_input.question_id == normalized_question_id:
                return parsed_input
        raise ValueError(
            f"`question_id` `{normalized_question_id}` was not found in list input {input_path}"
        )

    if len(parsed_inputs) == 1:
        return parsed_inputs[0]

    raise ValueError(
        f"Input file {input_path} contains a JSON list with {len(parsed_inputs)} items. "
        "Provide --question-id to select one item."
    )


def read_input_payloads(
    path: str | Path,
    *,
    require_question_id: bool = True,
    allow_question_id_from_db_id: bool = False,
) -> list[NL2ERInput]:
    input_path = Path(path)
    payload = json.loads(input_path.read_text(encoding="utf-8"))
    raw_items = payload if isinstance(payload, list) else [payload]
    if not isinstance(raw_items, list):
        raise ValueError(f"Expected JSON object or JSON list at {input_path}")

    inputs: list[NL2ERInput] = []
    seen_question_ids: set[str] = set()
    for index, item in enumerate(raw_items):
        parsed = _parse_input_payload_object(
            item,
            input_path=input_path,
            location=f"{input_path}[{index}]" if isinstance(payload, list) else str(input_path),
            require_question_id=require_question_id,
            allow_question_id_from_db_id=allow_question_id_from_db_id,
        )
        if parsed.question_id in seen_question_ids:
            raise ValueError(f"Duplicate question_id `{parsed.question_id}` found in {input_path}")
        seen_question_ids.add(parsed.question_id)
        inputs.append(parsed)
    return inputs


def serialize_input_payload(input_payload: NL2ERInput) -> dict[str, str]:
    return {
        "question_id": input_payload.question_id,
        "user_intent": input_payload.user_intent,
        "db_id": input_payload.db_id,
        "db_hint": input_payload.db_hint,
        "external_knowledge": input_payload.external_knowledge,
    }


def _resolve_source_input_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if not resolved.is_absolute():
        return (Path.cwd() / resolved).resolve()
    return resolved.resolve()


def resolve_ground_truth_sql_path(
    *,
    source_input_path: str | Path,
    question_id: str,
) -> Path:
    return (
        _resolve_source_input_path(source_input_path).parent
        / "ground_truth"
        / f"{question_id}.sql"
    )


def read_ground_truth_sql(
    *,
    source_input_path: str | Path,
    question_id: str,
) -> str | None:
    normalized_question_id = str(question_id or "").strip()
    if not normalized_question_id:
        return None

    ground_truth_path = resolve_ground_truth_sql_path(
        source_input_path=source_input_path,
        question_id=normalized_question_id,
    )
    if not ground_truth_path.is_file():
        return None

    return ground_truth_path.read_text(encoding="utf-8")


def write_case_metadata(
    *,
    metadata_dir: str | Path,
    serialized_input: dict[str, Any],
    source_input_path: str | Path,
    question_id: str,
) -> None:
    resolved_metadata_dir = Path(metadata_dir)
    resolved_metadata_dir.mkdir(parents=True, exist_ok=True)
    (resolved_metadata_dir / "input.json").write_text(
        json.dumps(serialized_input, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    ground_truth_sql = read_ground_truth_sql(
        source_input_path=source_input_path,
        question_id=question_id,
    )
    if ground_truth_sql is None:
        return

    (resolved_metadata_dir / GROUND_TRUTH_OUTPUT_FILENAME).write_text(
        ground_truth_sql,
        encoding="utf-8",
    )
