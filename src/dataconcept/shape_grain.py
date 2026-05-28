from __future__ import annotations

from pathlib import Path
from typing import Any

from src.config import load_settings
from src.config.schema import LLMConfig
from src.dataconcept.models import (
    DataSnapshotEvidence,
    DataUnitGrainJudgment,
    DataUnitShapeGrainAnalysis,
    DataUnitShapeJudgment,
)
from src.llm.llm_client import LLMClient
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse


DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
TEMPLATE_KEY = "dataconcept_shape_grain"
TEMPLATE_NAME = "DataConcept_shape_grain_analysis_v0.1.md"


def _unique_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = value
    else:
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _first_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _normalize_shape_type(value: Any) -> str:
    text = _first_string(value, "unknown")
    if text == "relationship_bridge":
        return "relationship"
    return text


def _column_resolver(snapshot: DataSnapshotEvidence):
    by_fullname = {
        column.column_fullname.casefold(): column.column_fullname
        for column in snapshot.columns
        if column.column_fullname
    }
    by_name = {
        column.column_name.casefold(): column.column_fullname
        for column in snapshot.columns
        if column.column_name and column.column_fullname
    }

    def resolve(values: Any) -> list[str]:
        resolved_values: list[str] = []
        seen: set[str] = set()
        for value in _unique_strings(values):
            resolved = by_fullname.get(value.casefold()) or by_name.get(value.casefold()) or value
            if resolved in seen:
                continue
            resolved_values.append(resolved)
            seen.add(resolved)
        return resolved_values

    return resolve


def _normalize_identifier_sets(value: Any, resolve_columns) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    output: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        output.append(
            {
                "identifier_type": _first_string(
                    item.get("identifier_type"),
                    item.get("type"),
                    "unknown",
                ),
                "columns": resolve_columns(item.get("columns")),
                "definition": _first_string(item.get("definition"), item.get("description")),
            }
        )
    return output


def _normalize_entity(value: Any, resolve_columns) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    entity = dict(value)
    entity["representative_columns"] = resolve_columns(value.get("representative_columns"))
    return entity


def _normalize_relationship(value: Any, resolve_columns) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    relationship = dict(value)
    relationship["representative_columns"] = resolve_columns(value.get("representative_columns"))
    participants: list[dict[str, Any]] = []
    for participant in value.get("participant_entities") or []:
        if not isinstance(participant, dict):
            continue
        normalized = dict(participant)
        normalized["identifier_columns"] = resolve_columns(participant.get("identifier_columns"))
        participants.append(normalized)
    relationship["participant_entities"] = participants
    return relationship


def compact_snapshot_for_shape_grain(snapshot: DataSnapshotEvidence) -> dict[str, Any]:
    return {
        "snapshot_id": snapshot.snapshot_id,
        "source_kind": snapshot.source_kind,
        "table_fullname": snapshot.table_fullname,
        "table_name": snapshot.table_name,
        "table_description": snapshot.table_description,
        "member_tables": snapshot.member_tables,
        "columns": [
            {
                "column_fullname": column.column_fullname,
                "column_name": column.column_name,
                "data_type": column.data_type,
                "description": column.description,
                "sample_values": column.sample_values,
                "sample_stats": column.sample_stats.to_dict() if column.sample_stats else {},
            }
            for column in snapshot.columns
        ],
        "sample_rows": snapshot.sample_rows,
        "provided_constraints": snapshot.explicit_constraints,
        "relation_clues": [clue.to_dict() for clue in snapshot.relation_clues],
        "snapshot_warnings": snapshot.quality_warnings,
    }


def extract_shape_grain_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "data_unit_shape_grain_analysis",
        "shape_grain_analysis",
        "data_concept_shape_grain",
        "analysis",
        "result",
    ):
        value = parsed.get(key)
        if isinstance(value, dict):
            return value
    return parsed


def build_unknown_shape_grain_analysis(
    *,
    snapshot: DataSnapshotEvidence,
    reason: str,
    prompt_path: str = "",
    response_path: str = "",
    error: str = "",
) -> DataUnitShapeGrainAnalysis:
    notes = _unique_strings([*snapshot.quality_warnings, reason])[:3]
    return DataUnitShapeGrainAnalysis(
        snapshot_id=snapshot.snapshot_id,
        table_fullname=snapshot.table_fullname,
        source_kind=snapshot.source_kind,
        shape=DataUnitShapeJudgment(
            shape_type="unknown",
            description=reason,
            confidence="low",
        ),
        grain=DataUnitGrainJudgment(
            grain_kind="unknown",
            definition=reason,
        ),
        analysis_notes=notes,
        prompt_path=prompt_path,
        response_path=response_path,
        error=error,
    )


def normalize_shape_grain_analysis(
    *,
    parsed: dict[str, Any],
    snapshot: DataSnapshotEvidence,
    prompt_path: str = "",
    response_path: str = "",
) -> DataUnitShapeGrainAnalysis:
    payload = extract_shape_grain_payload(parsed)
    shape_payload = payload.get("shape") if isinstance(payload.get("shape"), dict) else {}
    grain_payload = payload.get("grain") if isinstance(payload.get("grain"), dict) else {}
    resolve_columns = _column_resolver(snapshot)
    notes = _unique_strings(
        payload.get("analysis_notes")
        or payload.get("notes")
        or payload.get("warnings")
    )[:3]
    return DataUnitShapeGrainAnalysis(
        snapshot_id=_first_string(payload.get("snapshot_id"), snapshot.snapshot_id),
        table_fullname=_first_string(payload.get("table_fullname"), snapshot.table_fullname),
        source_kind=snapshot.source_kind,
        shape=DataUnitShapeJudgment(
            shape_type=_normalize_shape_type(shape_payload.get("shape_type") or shape_payload.get("type")),
            description=_first_string(shape_payload.get("description"), shape_payload.get("rationale")),
            confidence=_first_string(shape_payload.get("confidence"), "low"),
        ),
        grain=DataUnitGrainJudgment(
            grain_kind=_first_string(grain_payload.get("grain_kind"), grain_payload.get("kind"), "unknown"),
            grain_name=_first_string(grain_payload.get("grain_name"), grain_payload.get("name")),
            definition=_first_string(
                grain_payload.get("definition"),
                grain_payload.get("description"),
                grain_payload.get("rationale"),
            ),
            identifier_sets=_normalize_identifier_sets(
                grain_payload.get("identifier_sets"),
                resolve_columns,
            ),
            entity=_normalize_entity(grain_payload.get("entity"), resolve_columns),
            relationship=_normalize_relationship(grain_payload.get("relationship"), resolve_columns),
        ),
        analysis_notes=_unique_strings([*snapshot.quality_warnings, *notes])[:3],
        prompt_path=prompt_path,
        response_path=response_path,
    )


class ShapeGrainAnalyzer:
    def __init__(
        self,
        *,
        prompt_dir: str | Path = DEFAULT_PROMPT_DIR,
        model_config: str | None = None,
        llm_config: LLMConfig | None = None,
        dry_run: bool = False,
        llm: LLMClient | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.model_config = model_config
        self.dry_run = dry_run
        self.llm = llm
        if not self.dry_run and self.llm is None:
            config = llm_config
            if config is None:
                settings = load_settings()
                config = settings.llm.get(model_config)
            self.llm = LLMClient(config)
        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self.prompt_builder.register_template(
            name=TEMPLATE_KEY,
            template_name=TEMPLATE_NAME,
            required_vars=["data_snapshot"],
        )

    def build_prompt(self, snapshot: DataSnapshotEvidence) -> str:
        return self.prompt_builder.build_text(
            TEMPLATE_KEY,
            vars={"data_snapshot": compact_snapshot_for_shape_grain(snapshot)},
        )

    def analyze_snapshot(
        self,
        snapshot: DataSnapshotEvidence,
        *,
        prompt_path: str = "",
        response_path: str = "",
    ) -> DataUnitShapeGrainAnalysis:
        prompt = self.build_prompt(snapshot)
        if prompt_path:
            Path(prompt_path).write_text(prompt, encoding="utf-8")
        if self.dry_run:
            return build_unknown_shape_grain_analysis(
                snapshot=snapshot,
                reason="Dry-run placeholder: LLM shape and grain analysis was not performed.",
                prompt_path=prompt_path,
                response_path=response_path,
            )
        if self.llm is None:
            raise RuntimeError("LLM client is not initialized.")
        raw_response = self.llm.single_turn(prompt, check_func=json_check)
        if response_path:
            Path(response_path).write_text(raw_response or "", encoding="utf-8")
        if not raw_response or not raw_response.strip():
            return build_unknown_shape_grain_analysis(
                snapshot=snapshot,
                reason="LLM returned empty response.",
                prompt_path=prompt_path,
                response_path=response_path,
                error="empty_response",
            )
        try:
            parsed = json_parse(raw_response)
        except Exception as exc:
            return build_unknown_shape_grain_analysis(
                snapshot=snapshot,
                reason="Failed to parse LLM shape and grain analysis response.",
                prompt_path=prompt_path,
                response_path=response_path,
                error=str(exc),
            )
        return normalize_shape_grain_analysis(
            parsed=parsed,
            snapshot=snapshot,
            prompt_path=prompt_path,
            response_path=response_path,
        )
