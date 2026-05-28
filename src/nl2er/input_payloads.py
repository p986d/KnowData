from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class InputPayload:
    question_id: str
    user_intent: str
    db_id: str
    db_hint: str = ""
    external_knowledge: str = ""


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.")
    return parsed


def read_inputs(path: str | Path) -> list[InputPayload]:
    file_path = Path(path)
    payload = json.loads(file_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        raw_items: list[Any] = [payload]
    elif isinstance(payload, list):
        raw_items = payload
    else:
        raise ValueError(f"Expected input JSON object or list at {file_path}.")

    inputs: list[InputPayload] = []
    for index, item in enumerate(raw_items):
        if not isinstance(item, dict):
            continue
        question_id = str(item.get("question_id") or item.get("instance_id") or f"q{index + 1}").strip()
        inputs.append(
            InputPayload(
                question_id=question_id,
                user_intent=str(item.get("user_intent") or item.get("instruction") or "").strip(),
                db_id=str(item.get("db_id") or "").strip(),
                db_hint=str(item.get("db_hint") or "").strip(),
                external_knowledge=str(item.get("external_knowledge") or "").strip(),
            )
        )
    return inputs


def select_inputs(inputs: list[InputPayload], requested_question_ids: list[str]) -> list[InputPayload]:
    if not requested_question_ids:
        return inputs
    requested = {str(item).casefold() for item in requested_question_ids}
    return [item for item in inputs if item.question_id.casefold() in requested]
