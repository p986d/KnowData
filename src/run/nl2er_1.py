from __future__ import annotations

import argparse
import json
import re
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import (
    build_timestamp,
    emit_step_done_log,
    format_elapsed_seconds,
    resolve_output_path,
    resolve_run_dir,
    resolve_run_log_dir,
    write_json,
)
from src.utils.sqlglot_parser import SqlglotParser


DEFAULT_INPUT_PATH = Path("data/input.json")
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/nl2er")
DEFAULT_METADATA_ROOT = Path("metadata")
DEFAULT_OUTPUT_FILENAME = "nl2er_output.json"
GROUND_TRUTH_OUTPUT_FILENAME = "ground_truth.sql"
CONCEPTUAL_SQL_PARSE_OUTPUT_FILENAME = "conceptual_sql_parse.json"
_NAME_FUZZY_RE = re.compile(r"[^0-9a-z]+")


@dataclass(slots=True)
class NL2ERInput:
    question_id: str
    user_intent: str
    db_id: str
    db_hint: str = ""
    external_knowledge: str = ""


class ERSkeletonIntegrityError(ValueError):
    def __init__(
        self,
        *,
        integrity_report: dict[str, Any],
        result: dict[str, Any],
    ) -> None:
        self.integrity_report = integrity_report
        self.result = result

        error_excerpt = "; ".join(
            str(item).strip()
            for item in list(integrity_report.get("errors") or [])[:3]
            if str(item).strip()
        )
        message = "NL2ER ER skeleton integrity validation failed."
        if error_excerpt:
            message = f"{message} {error_excerpt}"
        super().__init__(message)


def read_input_payload(path: str | Path) -> NL2ERInput:
    input_path = Path(path)
    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {input_path}, got {type(payload).__name__}")

    question_id = str(payload.get("question_id") or payload.get("instance_id") or "").strip()
    user_intent = str(payload.get("user_intent") or "").strip()
    db_id = str(payload.get("db_id") or "").strip()
    db_hint = payload.get("db_hint", "")
    external_knowledge = payload.get("external_knowledge", "")

    if not user_intent:
        raise ValueError(f"`user_intent` is required in {input_path}")
    if not db_id:
        raise ValueError(f"`db_id` is required in {input_path}")
    if not isinstance(db_hint, str):
        raise ValueError(f"`db_hint` must be a string in {input_path}")
    if not isinstance(external_knowledge, str):
        raise ValueError(f"`external_knowledge` must be a string in {input_path}")

    return NL2ERInput(
        question_id=question_id,
        user_intent=user_intent,
        db_id=db_id,
        db_hint=db_hint,
        external_knowledge=external_knowledge,
    )


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
        resolved = (Path.cwd() / resolved).resolve()
    else:
        resolved = resolved.resolve()
    return resolved


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
    write_json(resolved_metadata_dir / "input.json", serialized_input)

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


def write_conceptual_sql_parse_metadata(
    *,
    metadata_dir: str | Path,
    conceptual_sql_parse: dict[str, Any],
) -> None:
    write_json(
        Path(metadata_dir) / CONCEPTUAL_SQL_PARSE_OUTPUT_FILENAME,
        conceptual_sql_parse,
    )


def build_output_payload(
    *,
    input_payload: NL2ERInput,
    er_result: dict[str, Any],
) -> dict[str, Any]:
    integrity_report = er_result.get("integrity_report")
    compact_integrity_report: dict[str, Any] = {
        "passed": False,
        "summary": "",
        "errors": [],
        "warnings": [],
    }
    if isinstance(integrity_report, dict):
        compact_integrity_report = {
            "passed": bool(integrity_report.get("passed")),
            "summary": str(integrity_report.get("summary") or "").strip(),
            "errors": [
                str(item).strip()
                for item in list(integrity_report.get("errors") or [])
                if str(item).strip()
            ],
            "warnings": [
                str(item).strip()
                for item in list(integrity_report.get("warnings") or [])
                if str(item).strip()
            ],
        }

    return {
        "conceptual_sql": str(er_result.get("conceptual_sql") or "").strip(),
        "entities": list(er_result.get("entities") or []),
        "relations": list(er_result.get("relations") or []),
        "connections": list(er_result.get("connections") or []),
        "conditions": list(er_result.get("conditions") or []),
        "integrity_report": compact_integrity_report,
    }


class NL2ER:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload

        settings = load_settings()
        self.llm = LLMClient(settings.llm.get(model_config))
        self.build_prompt = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    def extract_erc(self) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Conceptual_st1_v7.25.md" #"NL2ER_ER_st1_v6.1.md"
        self.build_prompt.register_template(
            name="step_1_extract_ERC",
            template_name=template_name,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Extract the conceptual virtual SQL and ER skeleton from the user intent.",
        )
        prompt = self.build_prompt.build_text(
            "step_1_extract_ERC",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
            },
        )

        step = 1
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        self._write_text_log(f"response_{step}.md", response)

        if not response.strip():
            raise RuntimeError(
                "NL2ER LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return json_parse(response)

    @staticmethod
    def normalize_conceptual_query_plan(payload: dict[str, Any]) -> dict[str, Any]:
        conceptual_sql = str(payload.get("conceptual_sql") or "").strip()
        if not conceptual_sql:
            raise ValueError("NL2ER conceptual query plan is missing `conceptual_sql`.")
        return {
            "conceptual_sql": conceptual_sql,
            "conceptual_sql_parse": NL2ER.parse_stage1_conceptual_sql(conceptual_sql),
        }

    def conceptual_query_plan(self) -> dict[str, Any]:
        erc_payload = self.extract_erc()
        return self.normalize_conceptual_query_plan(erc_payload)

    @staticmethod
    def parse_stage1_conceptual_sql(conceptual_sql: str) -> dict[str, Any]:
        try:
            return SqlglotParser.extract_table_columns_and_join_conditions(conceptual_sql)
        except ValueError as exc:
            return {
                "tables": [],
                "join_conditions": [],
                "error": str(exc),
            }

    def parse_conceptual_sql(self, conceptual_sql: str) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Parse_st2_v7.0.md"
        self.build_prompt.register_template(
            name="step_2_parse_conceptual_sql",
            template_name=template_name,
            required_vars=["user_intent", "conceptual_sql"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Parse conceptual SQL into the raw ER retrieval spec fields.",
        )
        prompt = self.build_prompt.build_text(
            "step_2_parse_conceptual_sql",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
                "conceptual_sql": conceptual_sql,
            },
        )

        step = 2
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        self._write_text_log(f"response_{step}.md", response)

        if not response.strip():
            raise RuntimeError(
                "NL2ER SQL parse LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return self.normalize_sql_parse_output(json_parse(response))

    @staticmethod
    def normalize_sql_parse_output(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(
                f"Expected SQL parse JSON object, got {type(payload).__name__}"
            )

        return {
            "entities": NL2ER._normalize_entities(payload.get("entities")),
            "relations": NL2ER._normalize_units(
                payload.get("relations"),
                unit_type="relation",
            ),
            "connections": NL2ER._normalize_units(
                payload.get("connections"),
                unit_type="connection",
            ),
            "conditions": NL2ER._normalize_conditions(payload.get("conditions")),
        }

    @staticmethod
    def _extract_string_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []

        normalized: list[str] = []
        seen: set[str] = set()
        for item in value:
            text = ""
            if isinstance(item, str):
                text = item.strip()
            elif isinstance(item, dict):
                text = str(item.get("name") or "").strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    @staticmethod
    def _normalize_string_list(
        value: Any,
        *,
        location: str,
        field_name: str,
    ) -> list[str]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"`{field_name}` must be a list in {location}")

        normalized: list[str] = []
        seen: set[str] = set()
        for index, item in enumerate(value):
            if not isinstance(item, str):
                raise ValueError(
                    f"`{field_name}[{index}]` must be a string in {location}"
                )
            text = item.strip()
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    @classmethod
    def _normalize_entities(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("`entities` must be a list.")

        normalized_entities: list[dict[str, Any]] = []
        for index, entity in enumerate(value):
            location = f"entities[{index}]"
            if not isinstance(entity, dict):
                raise ValueError(f"`{location}` must be an object.")

            entity_name = str(entity.get("entity_name") or "").strip()
            if not entity_name:
                raise ValueError(f"`{location}.entity_name` is required.")

            attributes = cls._normalize_string_list(
                entity.get("attributes"),
                location=location,
                field_name="attributes",
            )
            primary_key = cls._normalize_string_list(
                entity.get("primary_key"),
                location=location,
                field_name="primary_key",
            )

            merged_attributes = list(attributes)
            existing_attrs = set(merged_attributes)
            for attr_name in primary_key:
                if attr_name in existing_attrs:
                    continue
                merged_attributes.append(attr_name)
                existing_attrs.add(attr_name)

            normalized_entities.append(
                {
                    "entity_name": entity_name,
                    "grain": str(entity.get("grain") or "").strip(),
                    "attributes": merged_attributes,
                    "primary_key": primary_key,
                }
            )

        return normalized_entities

    @classmethod
    def _normalize_participants(
        cls,
        value: Any,
        *,
        location: str,
    ) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"`participants` must be a list in {location}")

        normalized_participants: list[dict[str, Any]] = []
        for index, participant in enumerate(value):
            participant_location = f"{location}.participants[{index}]"
            if not isinstance(participant, dict):
                raise ValueError(f"`{participant_location}` must be an object.")

            entity_name = str(participant.get("entity") or "").strip()
            if not entity_name:
                raise ValueError(f"`{participant_location}.entity` is required.")

            normalized_participants.append(
                {
                    "role": str(participant.get("role") or "").strip(),
                    "entity": entity_name,
                    "anchor_attribute": cls._normalize_string_list(
                        participant.get("anchor_attribute"),
                        location=participant_location,
                        field_name="anchor_attribute",
                    ),
                }
            )

        return normalized_participants

    @classmethod
    def _normalize_units(
        cls,
        value: Any,
        *,
        unit_type: str,
    ) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError(f"`{unit_type}s` must be a list.")

        unit_name_key = f"{unit_type}_name"
        normalized_units: list[dict[str, Any]] = []
        for index, unit in enumerate(value):
            location = f"{unit_type}s[{index}]"
            if not isinstance(unit, dict):
                raise ValueError(f"`{location}` must be an object.")

            unit_name = str(unit.get(unit_name_key) or "").strip()
            if not unit_name:
                raise ValueError(f"`{location}.{unit_name_key}` is required.")

            normalized_units.append(
                {
                    unit_name_key: unit_name,
                    "grain": str(unit.get("grain") or "").strip(),
                    "participants": cls._normalize_participants(
                        unit.get("participants"),
                        location=location,
                    ),
                    "link_condition": str(unit.get("link_condition") or "").strip(),
                    "attributes": cls._normalize_string_list(
                        unit.get("attributes"),
                        location=location,
                        field_name="attributes",
                    ),
                }
            )

        return normalized_units

    @classmethod
    def _normalize_conditions(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("`conditions` must be a list.")

        normalized_conditions: list[dict[str, Any]] = []
        for index, condition in enumerate(value):
            location = f"conditions[{index}]"
            if not isinstance(condition, dict):
                raise ValueError(f"`{location}` must be an object.")

            condition_name = str(condition.get("condition_name") or "").strip()
            if not condition_name:
                raise ValueError(f"`{location}.condition_name` is required.")

            normalized_conditions.append(
                {
                    "condition_name": condition_name,
                    "condition_type": str(condition.get("condition_type") or "").strip(),
                    "targets": cls._normalize_string_list(
                        condition.get("targets"),
                        location=location,
                        field_name="targets",
                    ),
                    "description": str(condition.get("description") or "").strip(),
                }
            )

        return normalized_conditions

    @staticmethod
    def _normalize_match_key(value: str) -> str:
        return _NAME_FUZZY_RE.sub("_", value.strip().casefold()).strip("_")

    @classmethod
    def _resolve_name_match(
        cls,
        raw_name: str,
        candidates: list[str],
    ) -> tuple[str | None, str | None, bool]:
        stripped = str(raw_name or "").strip()
        if not stripped:
            return None, None, False

        exact_matches = [candidate for candidate in candidates if candidate == stripped]
        if len(exact_matches) == 1:
            return exact_matches[0], "exact", False
        if len(exact_matches) > 1:
            return None, "exact", True

        casefold_matches = [
            candidate for candidate in candidates if candidate.casefold() == stripped.casefold()
        ]
        if len(casefold_matches) == 1:
            return casefold_matches[0], "casefold", False
        if len(casefold_matches) > 1:
            return None, "casefold", True

        normalized_key = cls._normalize_match_key(stripped)
        if not normalized_key:
            return None, None, False

        normalized_matches = [
            candidate
            for candidate in candidates
            if cls._normalize_match_key(candidate) == normalized_key
        ]
        if len(normalized_matches) == 1:
            return normalized_matches[0], "normalized", False
        if len(normalized_matches) > 1:
            return None, "normalized", True

        return None, None, False

    @classmethod
    def _prune_invalid_connections(
        cls,
        er_payload: dict[str, Any],
    ) -> tuple[dict[str, Any], list[str]]:
        entities = er_payload.get("entities")
        relations = er_payload.get("relations")
        connections = er_payload.get("connections")

        if not isinstance(entities, list) or not isinstance(relations, list) or not isinstance(connections, list):
            return er_payload, []

        entity_names = [
            str(entity.get("entity_name") or "").strip()
            for entity in entities
            if isinstance(entity, dict) and str(entity.get("entity_name") or "").strip()
        ]
        relation_names = [
            str(relation.get("relation_name") or "").strip()
            for relation in relations
            if isinstance(relation, dict) and str(relation.get("relation_name") or "").strip()
        ]
        connection_names = [
            str(connection.get("connection_name") or "").strip()
            for connection in connections
            if isinstance(connection, dict) and str(connection.get("connection_name") or "").strip()
        ]

        kept_connections: list[dict[str, Any]] = []
        warnings: list[str] = []

        for index, connection in enumerate(connections):
            if not isinstance(connection, dict):
                kept_connections.append(connection)
                continue

            connection_name = str(connection.get("connection_name") or "").strip() or f"connections[{index}]"
            participants = connection.get("participants")
            if not isinstance(participants, list):
                kept_connections.append(connection)
                continue

            drop_reasons: list[str] = []
            for participant in participants:
                if not isinstance(participant, dict):
                    continue

                participant_entity = str(participant.get("entity") or "").strip()
                if not participant_entity:
                    continue

                matched_entity, entity_match_mode, entity_ambiguous = cls._resolve_name_match(
                    participant_entity,
                    entity_names,
                )
                if matched_entity:
                    continue

                if entity_ambiguous:
                    drop_reasons.append(
                        f"participant `{participant_entity}` has ambiguous {entity_match_mode or 'fuzzy'} match across entities"
                    )
                    continue

                matched_relation, relation_match_mode, relation_ambiguous = cls._resolve_name_match(
                    participant_entity,
                    relation_names,
                )
                if matched_relation:
                    drop_reasons.append(
                        f"participant `{participant_entity}` matched relation `{matched_relation}` via {relation_match_mode}"
                    )
                    continue
                if relation_ambiguous:
                    drop_reasons.append(
                        f"participant `{participant_entity}` has ambiguous {relation_match_mode or 'fuzzy'} match across relations"
                    )
                    continue

                matched_connection, connection_match_mode, connection_ambiguous = cls._resolve_name_match(
                    participant_entity,
                    connection_names,
                )
                if matched_connection:
                    drop_reasons.append(
                        f"participant `{participant_entity}` matched connection `{matched_connection}` via {connection_match_mode}"
                    )
                    continue
                if connection_ambiguous:
                    drop_reasons.append(
                        f"participant `{participant_entity}` has ambiguous {connection_match_mode or 'fuzzy'} match across connections"
                    )
                    continue

                drop_reasons.append(
                    f"participant `{participant_entity}` could not be matched to any entity"
                )

            if drop_reasons:
                warnings.append(
                    f"Dropped connection `{connection_name}` because "
                    + "; ".join(drop_reasons)
                    + "."
                )
                continue

            kept_connections.append(connection)

        sanitized_payload = dict(er_payload)
        sanitized_payload["connections"] = kept_connections
        return sanitized_payload, warnings

    def validate_er_skeleton_integrity(
        self,
        er_payload: dict[str, Any],
        *,
        pre_warnings: list[str] | None = None,
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = list(pre_warnings or [])

        raw_entities = er_payload.get("entities")
        raw_relations = er_payload.get("relations")
        raw_connections = er_payload.get("connections")

        entities = raw_entities if isinstance(raw_entities, list) else []
        relations = raw_relations if isinstance(raw_relations, list) else []
        connections = raw_connections if isinstance(raw_connections, list) else []

        if not isinstance(raw_entities, list):
            errors.append("Field `entities` must be a list.")
        if not isinstance(raw_relations, list):
            errors.append("Field `relations` must be a list.")
        if not isinstance(raw_connections, list):
            errors.append("Field `connections` must be a list.")

        entity_index: dict[str, dict[str, Any]] = {}
        entity_attrs: dict[str, set[str]] = {}

        for index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                errors.append(f"Entity at index {index} must be an object.")
                continue

            entity_name = str(entity.get("entity_name") or "").strip()
            if not entity_name:
                errors.append(f"Entity at index {index} is missing `entity_name`.")
                continue
            if entity_name in entity_index:
                errors.append(f"Duplicate entity name `{entity_name}`.")
                continue

            entity_index[entity_name] = entity
            entity_attrs[entity_name] = set(self._extract_string_list(entity.get("attributes")))

        adjacency: dict[str, set[str]] = {}
        all_nodes: set[str] = set()
        edge_count = 0

        def add_node(node_id: str) -> None:
            if not node_id:
                return
            all_nodes.add(node_id)
            adjacency.setdefault(node_id, set())

        def add_edge(left: str, right: str) -> None:
            nonlocal edge_count
            add_node(left)
            add_node(right)
            if right in adjacency[left]:
                return
            adjacency[left].add(right)
            adjacency[right].add(left)
            edge_count += 1

        for entity_name in entity_index:
            add_node(f"entity:{entity_name}")

        def validate_unit_collection(units: list[Any], *, unit_type: str) -> list[str]:
            unit_names: list[str] = []
            seen_names: set[str] = set()

            for index, unit in enumerate(units):
                if not isinstance(unit, dict):
                    errors.append(f"{unit_type.title()} at index {index} must be an object.")
                    continue

                unit_name = str(unit.get(f"{unit_type}_name") or "").strip()
                if not unit_name:
                    errors.append(
                        f"{unit_type.title()} at index {index} is missing `{unit_type}_name`."
                    )
                    continue
                if unit_name in seen_names:
                    errors.append(f"Duplicate {unit_type} name `{unit_name}`.")
                    continue

                seen_names.add(unit_name)
                unit_names.append(unit_name)
                unit_node = f"{unit_type}:{unit_name}"
                add_node(unit_node)

                participants = unit.get("participants")
                if not isinstance(participants, list):
                    errors.append(
                        f"{unit_type.title()} `{unit_name}` field `participants` must be a list."
                    )
                    continue
                if len(participants) < 2:
                    errors.append(
                        f"{unit_type.title()} `{unit_name}` must reference at least 2 participants."
                    )

                participant_entities: list[str] = []
                participant_roles: set[str] = set()

                for participant_index, participant in enumerate(participants):
                    if not isinstance(participant, dict):
                        errors.append(
                            f"{unit_type.title()} `{unit_name}` participant at index {participant_index} must be an object."
                        )
                        continue

                    participant_entity = str(participant.get("entity") or "").strip()
                    if not participant_entity:
                        errors.append(
                            f"{unit_type.title()} `{unit_name}` participant at index {participant_index} is missing `entity`."
                        )
                        continue

                    participant_entities.append(participant_entity)
                    if participant_entity not in entity_index:
                        errors.append(
                            f"{unit_type.title()} `{unit_name}` references unknown entity `{participant_entity}`."
                        )
                    else:
                        add_edge(unit_node, f"entity:{participant_entity}")

                    raw_anchor_attributes = participant.get("anchor_attribute")
                    if raw_anchor_attributes is not None and not isinstance(raw_anchor_attributes, list):
                        errors.append(
                            f"{unit_type.title()} `{unit_name}` participant `{participant_entity}` field `anchor_attribute` must be a list."
                        )
                        anchor_attributes = []
                    else:
                        anchor_attributes = self._extract_string_list(raw_anchor_attributes)
                    if not anchor_attributes:
                        errors.append(
                            f"{unit_type.title()} `{unit_name}` participant `{participant_entity}` is missing `anchor_attribute`."
                        )
                    elif participant_entity in entity_attrs and entity_attrs[participant_entity]:
                        unknown_anchors = [
                            attr
                            for attr in anchor_attributes
                            if attr not in entity_attrs[participant_entity]
                        ]
                        if unknown_anchors:
                            errors.append(
                                f"{unit_type.title()} `{unit_name}` participant `{participant_entity}` uses unknown anchor attributes: {', '.join(unknown_anchors)}."
                            )

                    role_name = str(participant.get("role") or "").strip()
                    if role_name:
                        if role_name in participant_roles:
                            warnings.append(
                                f"{unit_type.title()} `{unit_name}` reuses role `{role_name}` across participants."
                            )
                        participant_roles.add(role_name)

                if len(participant_entities) >= 2 and len(set(participant_entities)) == 1:
                    distinct_roles = {
                        str(participant.get("role") or "").strip()
                        for participant in participants
                        if isinstance(participant, dict) and str(participant.get("role") or "").strip()
                    }
                    if len(distinct_roles) < len(participant_entities):
                        warnings.append(
                            f"{unit_type.title()} `{unit_name}` is self-referential and some participant roles are not distinct."
                        )

            return unit_names

        relation_names = validate_unit_collection(relations, unit_type="relation")
        connection_names = validate_unit_collection(connections, unit_type="connection")

        components: list[dict[str, list[str]]] = []
        remaining_nodes = set(all_nodes)
        while remaining_nodes:
            start_node = next(iter(remaining_nodes))
            stack = [start_node]
            visited: set[str] = set()
            while stack:
                node = stack.pop()
                if node in visited:
                    continue
                visited.add(node)
                stack.extend(neighbor for neighbor in adjacency.get(node, set()) if neighbor not in visited)

            remaining_nodes.difference_update(visited)
            component_entities = sorted(
                node.split(":", 1)[1] for node in visited if node.startswith("entity:")
            )
            component_relations = sorted(
                node.split(":", 1)[1] for node in visited if node.startswith("relation:")
            )
            component_connections = sorted(
                node.split(":", 1)[1] for node in visited if node.startswith("connection:")
            )
            components.append(
                {
                    "entities": component_entities,
                    "relations": component_relations,
                    "connections": component_connections,
                }
            )

        named_entity_count = len(entity_index)
        named_relation_count = len(relation_names)
        named_connection_count = len(connection_names)

        if named_entity_count == 0:
            errors.append("ER skeleton must contain at least one valid entity.")
        elif named_entity_count == 1 and named_relation_count == 0 and named_connection_count == 0:
            warnings.append("ER skeleton contains only one entity and no relation/connection.")
        elif named_entity_count > 1 and named_relation_count == 0 and named_connection_count == 0:
            errors.append(
                "ER skeleton contains multiple entities but no relation/connection to connect them."
            )

        if len(components) > 1:
            errors.append(
                f"ER skeleton graph is disconnected and splits into {len(components)} components."
            )

        passed = not errors
        if passed:
            summary = (
                "ER skeleton integrity check passed."
                if not warnings
                else "ER skeleton integrity check passed with warnings."
            )
        else:
            summary = "ER skeleton integrity check failed."

        return {
            "passed": passed,
            "summary": summary,
            "errors": errors,
            "warnings": warnings,
            "stats": {
                "entity_count": named_entity_count,
                "relation_count": named_relation_count,
                "connection_count": named_connection_count,
                "node_count": len(all_nodes),
                "edge_count": edge_count,
                "component_count": len(components),
            },
            "graph": {
                "is_connected": len(components) <= 1 if all_nodes else False,
            },
            "components": components,
        }

    def run(self) -> dict[str, Any]:
        started_at = time.time()

        step_started_at = time.time()
        conceptual_plan = self.conceptual_query_plan()
        emit_step_done_log(
            prefix="NL2ER",
            step="conceptual_query_plan",
            elapsed_seconds=time.time() - step_started_at,
            has_conceptual_sql=bool(conceptual_plan.get("conceptual_sql")),
            parsed_table_count=len(
                list((conceptual_plan.get("conceptual_sql_parse") or {}).get("tables") or [])
            ),
            parsed_join_condition_count=len(
                list(
                    (conceptual_plan.get("conceptual_sql_parse") or {}).get("join_conditions")
                    or []
                )
            ),
        )

        step_started_at = time.time()
        sql_parse_result = self.parse_conceptual_sql(conceptual_plan["conceptual_sql"])
        emit_step_done_log(
            prefix="NL2ER",
            step="parse_conceptual_sql",
            elapsed_seconds=time.time() - step_started_at,
            entity_count=len(sql_parse_result.get("entities") or []),
            relation_count=len(sql_parse_result.get("relations") or []),
            connection_count=len(sql_parse_result.get("connections") or []),
            condition_count=len(sql_parse_result.get("conditions") or []),
        )

        step_started_at = time.time()
        sql_parse_result, connection_cleanup_warnings = self._prune_invalid_connections(
            sql_parse_result
        )
        emit_step_done_log(
            prefix="NL2ER",
            step="prune_invalid_connections",
            elapsed_seconds=time.time() - step_started_at,
            connection_count=len(sql_parse_result.get("connections") or []),
            dropped_count=len(connection_cleanup_warnings),
        )

        step_started_at = time.time()
        integrity_report = self.validate_er_skeleton_integrity(
            sql_parse_result,
            pre_warnings=connection_cleanup_warnings,
        )
        write_json(self.log_dir / "er_skeleton_integrity.json", integrity_report)
        emit_step_done_log(
            prefix="NL2ER",
            step="validate_er_skeleton_integrity",
            elapsed_seconds=time.time() - step_started_at,
            ok=integrity_report["passed"],
            passed=integrity_report["passed"],
            error_count=len(integrity_report["errors"]),
            warning_count=len(integrity_report["warnings"]),
            component_count=integrity_report["stats"]["component_count"],
        )
        result = {
            "conceptual_sql": conceptual_plan["conceptual_sql"],
            "conceptual_sql_parse": conceptual_plan.get("conceptual_sql_parse", {}),
            "entities": sql_parse_result.get("entities", []),
            "relations": sql_parse_result.get("relations", []),
            "connections": sql_parse_result.get("connections", []),
            "conditions": sql_parse_result.get("conditions", []),
        }
        if not integrity_report["passed"]:
            raise ERSkeletonIntegrityError(
                integrity_report=integrity_report,
                result=result,
            )

        elapsed_seconds = time.time() - started_at
        return {
            "result": result,
            "integrity_report": integrity_report,
            "elapsed_seconds": elapsed_seconds,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NL2ER from a parameterized JSON input.")
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--model-config", default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_payload = read_input_payload(args.input_path)
    if args.question_id:
        input_payload = replace(input_payload, question_id=str(args.question_id).strip())
    if args.db_id:
        input_payload = replace(input_payload, db_id=str(args.db_id).strip())

    if not input_payload.question_id:
        raise ValueError(
            "`question_id` is required. Provide it in the input JSON or pass --question-id."
        )

    run_timestamp = build_timestamp()
    run_id = f"{input_payload.question_id}_{run_timestamp}"
    log_dir = resolve_run_log_dir(
        run_prefix=input_payload.question_id,
        log_dir=args.log_dir,
        log_root=args.log_root,
        timestamp=run_timestamp,
    )
    metadata_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=args.metadata_dir,
        run_root=args.metadata_root,
        timestamp=run_timestamp,
    )
    output_path = resolve_output_path(
        output_path=args.output_path,
        run_dir=metadata_dir,
        default_filename=DEFAULT_OUTPUT_FILENAME,
    )

    serialized_input = serialize_input_payload(input_payload)
    write_case_metadata(
        metadata_dir=metadata_dir,
        serialized_input=serialized_input,
        source_input_path=args.input_path,
        question_id=input_payload.question_id,
    )
    write_json(
        log_dir / "input.json",
        serialized_input,
    )

    print(f"[NL2ER] run_id={run_id}")
    print(f"[NL2ER] question_id={input_payload.question_id}")
    print(f"[NL2ER] db_id={input_payload.db_id}")

    nl2er = NL2ER(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=input_payload,
        model_config=args.model_config,
    )
    started_at = time.time()
    try:
        payload = nl2er.run()
    except ERSkeletonIntegrityError as exc:
        elapsed_seconds = time.time() - started_at
        write_conceptual_sql_parse_metadata(
            metadata_dir=metadata_dir,
            conceptual_sql_parse=dict(exc.result.get("conceptual_sql_parse") or {}),
        )
        write_json(
            output_path,
            build_output_payload(
                input_payload=input_payload,
                er_result={
                    **exc.result,
                    "integrity_report": exc.integrity_report,
                },
            ),
        )
        print(f"[NL2ER] integrity check failed: {exc}")
        print(f"[NL2ER] elapsed={format_elapsed_seconds(elapsed_seconds)}")
        print(f"[NL2ER] output wrote to {output_path}")
        print(f"[NL2ER] logs wrote to {log_dir}")
        print(f"[NL2ER] metadata wrote to {metadata_dir}")
        raise SystemExit(2) from exc
    write_conceptual_sql_parse_metadata(
        metadata_dir=metadata_dir,
        conceptual_sql_parse=dict(payload["result"].get("conceptual_sql_parse") or {}),
    )
    write_json(
        output_path,
        build_output_payload(
            input_payload=input_payload,
            er_result={
                **payload["result"],
                "integrity_report": payload["integrity_report"],
            },
        ),
    )

    print(f"[NL2ER] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
    print(f"[NL2ER] output wrote to {output_path}")
    print(f"[NL2ER] logs wrote to {log_dir}")
    print(f"[NL2ER] metadata wrote to {metadata_dir}")


if __name__ == "__main__":
    main()
