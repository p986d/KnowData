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


DEFAULT_INPUT_PATH = Path("data/input.json")
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/nl2er")
DEFAULT_METADATA_ROOT = Path("metadata")
DEFAULT_OUTPUT_FILENAME = "nl2er_output.json"
ER_TEST_ST1_OUTPUT_FILENAME = "nl2er_er_test_st1_output.json"
ER_TEST_ST2_OUTPUT_FILENAME = DEFAULT_OUTPUT_FILENAME
GROUND_TRUTH_OUTPUT_FILENAME = "ground_truth.sql"
_NAME_FUZZY_RE = re.compile(r"[^0-9a-z]+")
_SQL_LIKE_RE = re.compile(
    r"(?is)\bselect\b.+\bfrom\b|\bgroup\s+by\b|\border\s+by\b|\bhaving\b|\blimit\b",
)
ATTRIBUTE_MODE_NAMES = "names"
ATTRIBUTE_MODE_LIST = "list"
ATTRIBUTE_MODE_DICT = "dict"
_NAMED_ITEM_KEYS = (
    "name",
    "attr",
    "attribute_name",
    "field_name",
    "key",
    "value",
)
_ATTRIBUTE_SEMANTIC_KEYS = (
    "semantics",
    "semantic",
    "desc",
    "description",
    "meaning",
)


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


def _parse_input_payload_object(
    payload: object,
    *,
    input_path: Path,
    location: str | None = None,
) -> NL2ERInput:
    resolved_location = location or str(input_path)
    if not isinstance(payload, dict):
        raise ValueError(
            f"Expected JSON object at {resolved_location}, got {type(payload).__name__}"
        )

    question_id = str(payload.get("question_id") or payload.get("instance_id") or "").strip()
    user_intent = str(payload.get("user_intent") or "").strip()
    db_id = str(payload.get("db_id") or "").strip()
    db_hint = payload.get("db_hint", "")
    external_knowledge = payload.get("external_knowledge", "")

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
    with input_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)

    if isinstance(payload, dict):
        return _parse_input_payload_object(payload, input_path=input_path)

    if not isinstance(payload, list):
        raise ValueError(
            f"Expected JSON object or JSON list at {input_path}, got {type(payload).__name__}"
        )

    if not payload:
        raise ValueError(f"Input list at {input_path} is empty.")

    normalized_question_id = str(question_id or "").strip()
    parsed_inputs: list[NL2ERInput] = []
    for index, item in enumerate(payload):
        parsed_inputs.append(
            _parse_input_payload_object(
                item,
                input_path=input_path,
                location=f"{input_path}[{index}]",
            )
        )

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


def build_output_payload(
    *,
    input_payload: NL2ERInput,
    er_result: dict[str, Any],
) -> dict[str, Any]:
    del input_payload

    direct_entities = er_result.get("entities")
    direct_relations = er_result.get("relations")
    direct_conditions = er_result.get("conditions")
    if (
        isinstance(direct_entities, list)
        or isinstance(direct_relations, list)
        or isinstance(direct_conditions, list)
    ):
        return {
            "entities": list(direct_entities or []),
            "relations": list(direct_relations or []),
            "conditions": list(direct_conditions or []),
        }

    refined_object_sketch = dict(er_result.get("refined_object_sketch") or {})
    constraints = list(er_result.get("constraints") or [])
    if refined_object_sketch:
        return {
            "entities": build_legacy_entities_payload(
                list(refined_object_sketch.get("entities") or [])
            ),
            "relations": build_legacy_relations_payload(
                list(refined_object_sketch.get("relations") or [])
            ),
            "conditions": build_legacy_conditions_payload(constraints),
        }
    return {"entities": [], "relations": [], "conditions": []}


def build_compact_integrity_report(integrity_report: Any) -> dict[str, Any]:
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
    return compact_integrity_report


def build_er_test_st1_output_payload(
    *,
    object_sketch: dict[str, Any],
    integrity_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "mode": "er_test_st1",
        "entities": list(object_sketch.get("entities") or []),
        "relations": list(object_sketch.get("relations") or []),
        "integrity_report": build_compact_integrity_report(integrity_report),
    }


def build_er_test_st2_output_payload(
    *,
    stage1_object_sketch: dict[str, Any],
    stage1_integrity_report: dict[str, Any] | None = None,
    semantic_review: dict[str, Any],
    integrity_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    del stage1_object_sketch
    del stage1_integrity_report
    del integrity_report

    return {
        "entities": list(semantic_review.get("entities") or []),
        "relations": list(semantic_review.get("relations") or []),
        "conditions": list(semantic_review.get("conditions") or []),
    }


def build_legacy_entities_payload(entities: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legacy_entities: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        entity_name = str(entity.get("name") or "").strip()
        if not entity_name:
            continue
        legacy_entities.append(
            {
                "entity_name": entity_name,
                "desc": str(entity.get("desc") or "").strip(),
                "grain": str(entity.get("grain") or "").strip(),
                "primary_key": list(entity.get("key_attributes") or []),
                "attributes": list(entity.get("attributes") or []),
            }
        )
    return legacy_entities


def build_legacy_relations_payload(relations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legacy_relations: list[dict[str, Any]] = []
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        relation_name = str(relation.get("name") or "").strip()
        if not relation_name:
            continue
        legacy_relations.append(
            {
                "relation_name": relation_name,
                "desc": str(relation.get("desc") or "").strip(),
                "grain": str(relation.get("grain") or "").strip(),
                "participants": list(relation.get("participants") or []),
                "attributes": list(relation.get("attributes") or []),
            }
        )
    return legacy_relations


def build_legacy_conditions_payload(constraints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    legacy_conditions: list[dict[str, Any]] = []
    for index, constraint in enumerate(constraints, start=1):
        if not isinstance(constraint, dict):
            continue
        target = str(constraint.get("target") or "").strip()
        condition_type = str(constraint.get("type") or "").strip()
        description = str(constraint.get("description") or "").strip()
        based_on = list(constraint.get("based_on") or [])
        condition_name = str(constraint.get("name") or "").strip()
        if not condition_name:
            suffix = target or f"constraint_{index}"
            condition_name = f"{suffix}__constraint_{index}"
        legacy_condition: dict[str, Any] = {
            "condition_name": condition_name,
            "targets": [target] if target else [],
            "condition_type": condition_type,
            "description": description,
        }
        if based_on:
            legacy_condition["based_on"] = based_on
        legacy_conditions.append(legacy_condition)
    return legacy_conditions


class NL2ER:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        stage2_attribute_mode: str = ATTRIBUTE_MODE_LIST,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.stage2_attribute_mode = self._normalize_attribute_mode(
            stage2_attribute_mode,
            supported_modes=(ATTRIBUTE_MODE_LIST, ATTRIBUTE_MODE_DICT),
        )

        settings = load_settings()
        self.llm = LLMClient(settings.llm.get(model_config))
        self.build_prompt = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    def extract_er_object_sketch(self) -> dict[str, Any]:
        template_name = "NL2ER_ER_test_st1_v0.16.md"
        self.build_prompt.register_template(
            name="step_1_extract_er_object_sketch",
            template_name=template_name,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Extract a lightweight ER object sketch via semantic dependency backtracking.",
        )
        prompt = self.build_prompt.build_text(
            "step_1_extract_er_object_sketch",
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
                "NL2ER ER test stage1 LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return self.normalize_er_object_sketch(json_parse(response))

    def review_er_object_sketch(self, stage1_object_sketch: dict[str, Any]) -> dict[str, Any]:
        template_name = "NL2ER_ER_test_st2_v0.14.md"
        self.build_prompt.register_template(
            name="step_2_review_er_object_sketch",
            template_name=template_name,
            required_vars=["user_intent", "stage1_object_sketch"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Review and refine the stage1 ER object sketch, add constraints, and build a logical query sketch.",
        )
        prompt = self.build_prompt.build_text(
            "step_2_review_er_object_sketch",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
                "stage1_object_sketch": json.dumps(
                    stage1_object_sketch,
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        )

        step = 2
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        self._write_text_log(f"response_{step}.md", response)

        if not response.strip():
            raise RuntimeError(
                "NL2ER ER test stage2 LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return self.normalize_er_semantic_review(
            json_parse(response),
            attribute_mode=self.stage2_attribute_mode,
        )

    def extract_query_units(self) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Conceptual_2_st1_v1.2.md"
        self.build_prompt.register_template(
            name="step_1_extract_query_units",
            template_name=template_name,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Extract named query units from the user intent.",
        )
        prompt = self.build_prompt.build_text(
            "step_1_extract_query_units",
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
                "NL2ER query unit extraction LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        return self.normalize_query_units_payload(json_parse(response))

    @staticmethod
    def normalize_query_units_payload(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(
                f"Expected query unit JSON object, got {type(payload).__name__}"
            )

        query_units = payload.get("query_units")
        if not isinstance(query_units, list):
            raise ValueError("NL2ER query unit extraction result is missing `query_units`.")

        normalized_query_units: list[dict[str, Any]] = []
        for index, query_unit in enumerate(query_units):
            if not isinstance(query_unit, dict):
                raise ValueError(f"`query_units[{index}]` must be an object.")
            normalized_query_units.append(query_unit)

        return {"query_units": normalized_query_units}

    @staticmethod
    def normalize_conceptual_query_plan(payload: dict[str, Any]) -> dict[str, Any]:
        conceptual_sql = str(payload.get("conceptual_sql") or "").strip()
        if not conceptual_sql:
            raise ValueError("NL2ER conceptual query plan is missing `conceptual_sql`.")
        return {"conceptual_sql": conceptual_sql}

    def conceptual_query_plan(self, query_units: list[dict[str, Any]]) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Conceptual_2_st2_v1.2.md"
        self.build_prompt.register_template(
            name="step_2_conceptual_query_plan",
            template_name=template_name,
            required_vars=["user_intent", "query_units"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Build the conceptual SQL from the named query units.",
        )
        prompt = self.build_prompt.build_text(
            "step_2_conceptual_query_plan",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
                "query_units": json.dumps(query_units, ensure_ascii=False, indent=2),
            },
        )

        step = 2
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        self._write_text_log(f"response_{step}.md", response)

        if not response.strip():
            raise RuntimeError(
                "NL2ER conceptual query plan LLM returned an empty response. Check model connectivity, credentials, or prompt validity."
            )

        conceptual_plan = self.normalize_conceptual_query_plan(json_parse(response))
        conceptual_plan["query_units"] = query_units
        return conceptual_plan

    @classmethod
    def _normalize_er_test_entities(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("`entities` must be a list.")

        normalized_entities: list[dict[str, Any]] = []
        for index, entity in enumerate(value):
            location = f"entities[{index}]"
            if not isinstance(entity, dict):
                raise ValueError(f"`{location}` must be an object.")

            name = str(entity.get("name") or "").strip()
            if not name:
                raise ValueError(f"`{location}.name` is required.")

            normalized_entities.append(
                {
                    "name": name,
                    "desc": str(entity.get("desc") or "").strip(),
                    "grain": str(entity.get("grain") or "").strip(),
                    "key_attributes": entity.get("key_attributes"),
                    "attributes": entity.get("attributes"),
                }
            )

        return normalized_entities

    @classmethod
    def _normalize_er_test_participants(
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
                    "entity": entity_name,
                    "role": str(participant.get("role") or "").strip(),
                }
            )

        return normalized_participants

    @classmethod
    def _normalize_er_test_relations(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("`relations` must be a list.")

        normalized_relations: list[dict[str, Any]] = []
        for index, relation in enumerate(value):
            location = f"relations[{index}]"
            if not isinstance(relation, dict):
                raise ValueError(f"`{location}` must be an object.")

            name = str(relation.get("name") or "").strip()
            if not name:
                raise ValueError(f"`{location}.name` is required.")

            normalized_relations.append(
                {
                    "name": name,
                    "desc": str(relation.get("desc") or "").strip(),
                    "grain": str(relation.get("grain") or "").strip(),
                    "participants": cls._normalize_er_test_participants(
                        relation.get("participants"),
                        location=location,
                    ),
                    "attributes": relation.get("attributes"),
                }
            )

        return normalized_relations

    @classmethod
    def normalize_er_object_sketch(cls, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(
                f"Expected ER object sketch JSON object, got {type(payload).__name__}"
            )

        return {
            "entities": cls._normalize_er_test_entities(payload.get("entities")),
            "relations": cls._normalize_er_test_relations(payload.get("relations")),
        }

    @classmethod
    def _normalize_er_review_constraints(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("`constraints` must be a list.")

        allowed_types = {
            "attribute_filter",
            "temporal",
            "spatial",
            "existence",
            "role",
            "set_membership",
            "distinctness",
        }
        normalized_constraints: list[dict[str, Any]] = []
        for index, constraint in enumerate(value):
            location = f"constraints[{index}]"
            if not isinstance(constraint, dict):
                raise ValueError(f"`{location}` must be an object.")

            target = str(constraint.get("target") or "").strip()
            if not target:
                raise ValueError(f"`{location}.target` is required.")

            constraint_type = str(constraint.get("type") or "").strip()
            if not constraint_type:
                raise ValueError(f"`{location}.type` is required.")
            if constraint_type not in allowed_types:
                raise ValueError(
                    f"`{location}.type` must be one of: {', '.join(sorted(allowed_types))}."
                )

            description = str(constraint.get("description") or "").strip()
            if not description:
                raise ValueError(f"`{location}.description` is required.")

            normalized_constraints.append(
                {
                    "target": target,
                    "type": constraint_type,
                    "description": description,
                    "based_on": cls._normalize_string_list(
                        constraint.get("based_on"),
                        location=location,
                        field_name="based_on",
                    ),
                }
            )

        return normalized_constraints

    @classmethod
    def _normalize_er_review_derived_metrics(cls, value: Any) -> list[dict[str, Any]]:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("`derived_metrics` must be a list.")

        normalized_metrics: list[dict[str, Any]] = []
        for index, metric in enumerate(value):
            location = f"derived_metrics[{index}]"
            if not isinstance(metric, dict):
                raise ValueError(f"`{location}` must be an object.")

            name = str(metric.get("name") or "").strip()
            if not name:
                raise ValueError(f"`{location}.name` is required.")

            definition = str(metric.get("definition") or "").strip()
            if not definition:
                raise ValueError(f"`{location}.definition` is required.")

            normalized_metrics.append(
                {
                    "name": name,
                    "definition": definition,
                    "depends_on": cls._normalize_string_list(
                        metric.get("depends_on"),
                        location=location,
                        field_name="depends_on",
                    ),
                }
            )

        return normalized_metrics

    @classmethod
    def _normalize_er_review_logical_query_sketch(cls, value: Any) -> dict[str, Any]:
        location = "logical_query_sketch"
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"`{location}` must be an object.")

        answer_grain = str(value.get("answer_grain") or "").strip()
        if not answer_grain:
            raise ValueError(f"`{location}.answer_grain` is required.")

        return {
            "answer_grain": answer_grain,
            "base_scope": cls._normalize_string_list(
                value.get("base_scope"),
                location=location,
                field_name="base_scope",
            ),
            "join_semantics": cls._normalize_string_list(
                value.get("join_semantics"),
                location=location,
                field_name="join_semantics",
            ),
            "filters": cls._normalize_string_list(
                value.get("filters"),
                location=location,
                field_name="filters",
            ),
            "group_by": cls._normalize_string_list(
                value.get("group_by"),
                location=location,
                field_name="group_by",
            ),
            "derived_metrics": cls._normalize_er_review_derived_metrics(
                value.get("derived_metrics")
            ),
            "ranking": cls._normalize_string_list(
                value.get("ranking"),
                location=location,
                field_name="ranking",
            ),
            "final_projection": cls._normalize_string_list(
                value.get("final_projection"),
                location=location,
                field_name="final_projection",
            ),
        }

    @classmethod
    def _normalize_er_review_report(cls, value: Any) -> dict[str, Any]:
        location = "review_report"
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise ValueError(f"`{location}` must be an object.")
        return dict(value)

    @classmethod
    def normalize_er_semantic_review(
        cls,
        payload: dict[str, Any],
        *,
        attribute_mode: str = ATTRIBUTE_MODE_LIST,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError(
                f"Expected ER semantic review JSON object, got {type(payload).__name__}"
            )

        normalized_attribute_mode = cls._normalize_attribute_mode(
            attribute_mode,
            supported_modes=(ATTRIBUTE_MODE_LIST, ATTRIBUTE_MODE_DICT),
        )

        if any(key in payload for key in ("entities", "relations", "conditions")):
            return {
                "entities": cls._normalize_entities(
                    payload.get("entities"),
                    attribute_mode=normalized_attribute_mode,
                ),
                "relations": cls._normalize_units(
                    payload.get("relations"),
                    unit_type="relation",
                    attribute_mode=normalized_attribute_mode,
                ),
                "conditions": cls._normalize_conditions(payload.get("conditions")),
            }

        refined_object_sketch = payload.get("refined_object_sketch")
        if not isinstance(refined_object_sketch, dict):
            raise ValueError(
                "ER semantic review output must contain either top-level "
                "`entities`/`relations`/`conditions` or `refined_object_sketch`."
            )

        return {
            "entities": build_legacy_entities_payload(
                cls._normalize_er_test_entities(refined_object_sketch.get("entities"))
            ),
            "relations": build_legacy_relations_payload(
                cls._normalize_er_test_relations(refined_object_sketch.get("relations"))
            ),
            "conditions": build_legacy_conditions_payload(
                cls._normalize_er_review_constraints(payload.get("constraints"))
            ),
        }

    def parse_conceptual_sql(self, conceptual_sql: str) -> dict[str, Any]:
        template_name = "NL2ER_SQL_Parse_st2_v7.0.md"
        self.build_prompt.register_template(
            name="step_3_parse_conceptual_sql",
            template_name=template_name,
            required_vars=["user_intent", "conceptual_sql"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Parse conceptual SQL into the raw ER retrieval spec fields.",
        )
        prompt = self.build_prompt.build_text(
            "step_3_parse_conceptual_sql",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
                "conceptual_sql": conceptual_sql,
            },
        )

        step = 3
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
            "entities": NL2ER._normalize_entities(
                payload.get("entities"),
                attribute_mode=ATTRIBUTE_MODE_NAMES,
            ),
            "relations": NL2ER._normalize_units(
                payload.get("relations"),
                unit_type="relation",
                attribute_mode=ATTRIBUTE_MODE_NAMES,
            ),
            "connections": NL2ER._normalize_units(
                payload.get("connections"),
                unit_type="connection",
                attribute_mode=ATTRIBUTE_MODE_NAMES,
            ),
            "conditions": NL2ER._normalize_conditions(payload.get("conditions")),
        }

    @classmethod
    def _extract_string_list(cls, value: Any) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        items: list[Any]
        if isinstance(value, list):
            items = list(value)
        elif isinstance(value, dict):
            items = list(value.keys())
        else:
            return []

        for item in items:
            text = cls._extract_named_item_name(item)
            if not text or text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    @staticmethod
    def _normalize_attribute_mode(
        attribute_mode: str | None,
        *,
        supported_modes: tuple[str, ...] = (
            ATTRIBUTE_MODE_NAMES,
            ATTRIBUTE_MODE_LIST,
            ATTRIBUTE_MODE_DICT,
        ),
    ) -> str:
        normalized_mode = str(attribute_mode or ATTRIBUTE_MODE_LIST).strip().casefold()
        if normalized_mode not in supported_modes:
            supported = ", ".join(f"`{mode}`" for mode in supported_modes)
            raise ValueError(f"`attribute_mode` must be one of {supported}.")
        return normalized_mode

    @staticmethod
    def _extract_named_item_name(item: Any) -> str:
        if isinstance(item, str):
            return item.strip()
        if not isinstance(item, dict):
            return ""

        for candidate_key in _NAMED_ITEM_KEYS:
            candidate_value = str(item.get(candidate_key) or "").strip()
            if candidate_value:
                return candidate_value

        if len(item) == 1:
            raw_name = next(iter(item.keys()), "")
            return str(raw_name or "").strip()

        return ""

    @classmethod
    def _parse_attribute_item(
        cls,
        item: Any,
        *,
        location: str,
        field_name: str,
        index: int | None = None,
    ) -> dict[str, str]:
        item_label = (
            f"`{field_name}[{index}]`" if index is not None else f"`{field_name}`"
        )
        if isinstance(item, str):
            text = item.strip()
            if not text:
                raise ValueError(f"{item_label} must not be empty in {location}")
            return {"name": text, "semantics": ""}
        if not isinstance(item, dict):
            raise ValueError(
                f"{item_label} must be a string, a named object, or a single-entry mapping in {location}"
            )

        name = cls._extract_named_item_name(item)
        semantics = ""
        if name:
            for candidate_key in _ATTRIBUTE_SEMANTIC_KEYS:
                candidate_value = item.get(candidate_key)
                if candidate_value is None:
                    continue
                candidate_text = str(candidate_value).strip()
                if candidate_text:
                    semantics = candidate_text
                    break
        elif len(item) == 1:
            raw_name, raw_semantics = next(iter(item.items()))
            name = str(raw_name or "").strip()
            if raw_semantics is not None:
                semantics = str(raw_semantics).strip()

        if not name:
            raise ValueError(
                f"{item_label} must be a string, a named object, or a single-entry mapping in {location}"
            )

        return {"name": name, "semantics": semantics}

    @classmethod
    def _normalize_attribute_records(
        cls,
        value: Any,
        *,
        location: str,
        field_name: str,
    ) -> list[dict[str, str]]:
        if value is None:
            return []

        normalized: list[dict[str, str]] = []
        index_by_name: dict[str, int] = {}

        def append_record(name: str, semantics: str) -> None:
            if not name:
                return
            existing_index = index_by_name.get(name)
            if existing_index is None:
                index_by_name[name] = len(normalized)
                normalized.append({"name": name, "semantics": semantics})
                return
            if semantics and not normalized[existing_index]["semantics"]:
                normalized[existing_index]["semantics"] = semantics

        if isinstance(value, list):
            for index, item in enumerate(value):
                record = cls._parse_attribute_item(
                    item,
                    location=location,
                    field_name=field_name,
                    index=index,
                )
                append_record(record["name"], record["semantics"])
            return normalized

        if isinstance(value, dict):
            for raw_name, raw_semantics in value.items():
                name = str(raw_name or "").strip()
                if not name:
                    continue
                semantics = "" if raw_semantics is None else str(raw_semantics).strip()
                append_record(name, semantics)
            return normalized

        raise ValueError(f"`{field_name}` must be a list or an object in {location}")

    @classmethod
    def _merge_attribute_records(
        cls,
        attribute_records: list[dict[str, str]],
        extra_names: list[str],
    ) -> list[dict[str, str]]:
        merged: list[dict[str, str]] = []
        seen: set[str] = set()
        for record in attribute_records:
            name = str(record.get("name") or "").strip()
            if not name or name in seen:
                continue
            merged.append(
                {
                    "name": name,
                    "semantics": str(record.get("semantics") or "").strip(),
                }
            )
            seen.add(name)

        for raw_name in extra_names:
            name = str(raw_name or "").strip()
            if not name or name in seen:
                continue
            merged.append({"name": name, "semantics": ""})
            seen.add(name)

        return merged

    @classmethod
    def _serialize_attribute_records(
        cls,
        attribute_records: list[dict[str, str]],
        *,
        attribute_mode: str,
    ) -> Any:
        normalized_mode = cls._normalize_attribute_mode(attribute_mode)
        if normalized_mode == ATTRIBUTE_MODE_NAMES:
            return [record["name"] for record in attribute_records]
        if normalized_mode == ATTRIBUTE_MODE_DICT:
            return {
                record["name"]: str(record.get("semantics") or "").strip()
                for record in attribute_records
            }
        return [
            {
                "name": record["name"],
                "semantics": str(record.get("semantics") or "").strip(),
            }
            for record in attribute_records
        ]

    @classmethod
    def _normalize_attributes(
        cls,
        value: Any,
        *,
        location: str,
        field_name: str,
        attribute_mode: str,
    ) -> Any:
        attribute_records = cls._normalize_attribute_records(
            value,
            location=location,
            field_name=field_name,
        )
        return cls._serialize_attribute_records(
            attribute_records,
            attribute_mode=attribute_mode,
        )

    @classmethod
    def _normalize_string_list(
        cls,
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
            text = cls._extract_named_item_name(item)
            if not text:
                raise ValueError(
                    f"`{field_name}[{index}]` must be a string or a named object in {location}"
                )
            if text in seen:
                continue
            normalized.append(text)
            seen.add(text)
        return normalized

    @classmethod
    def _normalize_entities(
        cls,
        value: Any,
        *,
        attribute_mode: str = ATTRIBUTE_MODE_NAMES,
    ) -> list[dict[str, Any]]:
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

            attribute_records = cls._normalize_attribute_records(
                entity.get("attributes"),
                location=location,
                field_name="attributes",
            )
            primary_key = cls._normalize_string_list(
                entity.get("primary_key"),
                location=location,
                field_name="primary_key",
            )

            merged_attributes = cls._serialize_attribute_records(
                cls._merge_attribute_records(attribute_records, primary_key),
                attribute_mode=attribute_mode,
            )

            normalized_entities.append(
                {
                    "entity_name": entity_name,
                    "desc": str(entity.get("desc") or "").strip(),
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
        attribute_mode: str = ATTRIBUTE_MODE_NAMES,
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
                    "desc": str(unit.get("desc") or "").strip(),
                    "grain": str(unit.get("grain") or "").strip(),
                    "participants": cls._normalize_participants(
                        unit.get("participants"),
                        location=location,
                    ),
                    "link_condition": str(unit.get("link_condition") or "").strip(),
                    "attributes": cls._normalize_attributes(
                        unit.get("attributes"),
                        location=location,
                        field_name="attributes",
                        attribute_mode=attribute_mode,
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

    def validate_er_object_sketch_integrity(
        self,
        object_sketch: dict[str, Any],
    ) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []

        entities = list(object_sketch.get("entities") or [])
        relations = list(object_sketch.get("relations") or [])

        entity_names: list[str] = []
        entity_name_set: set[str] = set()
        for entity in entities:
            entity_name = str(entity.get("name") or "").strip()
            if not entity_name:
                continue
            if entity_name in entity_name_set:
                errors.append(f"Entity `{entity_name}` is duplicated in object sketch.")
                continue
            entity_names.append(entity_name)
            entity_name_set.add(entity_name)
            raw_key_attributes = entity.get("key_attributes")
            if raw_key_attributes in (None, "", [], {}):
                warnings.append(
                    f"Entity `{entity_name}` does not declare any key_attributes."
                )

        relation_names: list[str] = []
        relation_name_set: set[str] = set()
        for relation in relations:
            relation_name = str(relation.get("name") or "").strip()
            if not relation_name:
                continue
            if relation_name in relation_name_set:
                errors.append(f"Relation `{relation_name}` is duplicated in object sketch.")
                continue
            relation_names.append(relation_name)
            relation_name_set.add(relation_name)

            participants = list(relation.get("participants") or [])
            if len(participants) < 2:
                errors.append(
                    f"Relation `{relation_name}` must reference at least 2 participants."
                )

            participant_roles: set[str] = set()
            participant_entities: list[str] = []
            for participant in participants:
                participant_entity = str(participant.get("entity") or "").strip()
                participant_role = str(participant.get("role") or "").strip()
                if not participant_entity:
                    errors.append(
                        f"Relation `{relation_name}` has a participant with empty entity."
                    )
                    continue
                participant_entities.append(participant_entity)
                if participant_entity not in entity_name_set:
                    errors.append(
                        f"Relation `{relation_name}` references unknown entity `{participant_entity}`."
                    )
                if participant_role:
                    if participant_role in participant_roles:
                        warnings.append(
                            f"Relation `{relation_name}` reuses role `{participant_role}` across participants."
                        )
                    participant_roles.add(participant_role)

            if len(participant_entities) >= 2 and len(set(participant_entities)) == 1:
                warnings.append(
                    f"Relation `{relation_name}` is self-referential; verify participant roles are distinct."
                )

        if not entity_names:
            errors.append("Object sketch must contain at least one entity.")

        passed = not errors
        if passed:
            summary = (
                "ER object sketch integrity check passed."
                if not warnings
                else "ER object sketch integrity check passed with warnings."
            )
        else:
            summary = "ER object sketch integrity check failed."

        return {
            "passed": passed,
            "summary": summary,
            "errors": errors,
            "warnings": warnings,
            "stats": {
                "entity_count": len(entity_names),
                "relation_count": len(relation_names),
            },
        }

    def validate_er_semantic_review_integrity(
        self,
        semantic_review: dict[str, Any],
    ) -> dict[str, Any]:
        entities = list(semantic_review.get("entities") or [])
        relations = list(semantic_review.get("relations") or [])
        conditions = list(semantic_review.get("conditions") or [])

        skeleton_integrity = self.validate_er_skeleton_integrity(
            {
                "entities": entities,
                "relations": relations,
                "connections": [],
                "conditions": conditions,
            }
        )
        errors: list[str] = list(skeleton_integrity.get("errors") or [])
        warnings: list[str] = list(skeleton_integrity.get("warnings") or [])

        entity_names = {
            str(entity.get("entity_name") or "").strip()
            for entity in entities
            if str(entity.get("entity_name") or "").strip()
        }
        relation_names = {
            str(relation.get("relation_name") or "").strip()
            for relation in relations
            if str(relation.get("relation_name") or "").strip()
        }
        role_names = {
            str(participant.get("role") or "").strip()
            for relation in relations
            if isinstance(relation, dict)
            for participant in list(relation.get("participants") or [])
            if isinstance(participant, dict) and str(participant.get("role") or "").strip()
        }
        valid_target_roots = entity_names | relation_names | role_names

        for condition in conditions:
            condition_name = str(condition.get("condition_name") or "").strip() or "<unnamed>"
            targets = list(condition.get("targets") or [])
            if not targets:
                warnings.append(f"Condition `{condition_name}` has no targets.")
                continue
            description = str(condition.get("description") or "").strip()
            if not description:
                warnings.append(f"Condition `{condition_name}` has empty description.")
            condition_type = str(condition.get("condition_type") or "").strip()
            if not condition_type:
                warnings.append(f"Condition `{condition_name}` has empty condition_type.")
            for target in targets:
                target_text = str(target or "").strip()
                target_root = target_text.split(".", 1)[0] if target_text else ""
                if target_root and target_root not in valid_target_roots:
                    warnings.append(
                        f"Condition `{condition_name}` target `{target_text}` does not match any entity, relation, or role."
                    )

        passed = not errors
        if passed:
            summary = (
                "ER semantic review integrity check passed."
                if not warnings
                else "ER semantic review integrity check passed with warnings."
            )
        else:
            summary = "ER semantic review integrity check failed."

        return {
            "passed": passed,
            "summary": summary,
            "errors": errors,
            "warnings": warnings,
            "stats": {
                "entity_count": len(entity_names),
                "relation_count": len(relation_names),
                "condition_count": len(conditions),
            },
        }

    def run_er_test_st1(self) -> dict[str, Any]:
        started_at = time.time()

        step_started_at = time.time()
        object_sketch = self.extract_er_object_sketch()
        write_json(self.log_dir / "er_object_sketch.json", object_sketch)
        emit_step_done_log(
            prefix="NL2ER",
            step="extract_er_object_sketch",
            elapsed_seconds=time.time() - step_started_at,
            entity_count=len(object_sketch.get("entities") or []),
            relation_count=len(object_sketch.get("relations") or []),
        )

        step_started_at = time.time()
        integrity_report = self.validate_er_object_sketch_integrity(object_sketch)
        write_json(self.log_dir / "er_object_sketch_integrity.json", integrity_report)
        emit_step_done_log(
            prefix="NL2ER",
            step="validate_er_object_sketch_integrity",
            elapsed_seconds=time.time() - step_started_at,
            ok=integrity_report["passed"],
            passed=integrity_report["passed"],
            error_count=len(integrity_report["errors"]),
            warning_count=len(integrity_report["warnings"]),
        )

        return {
            "result": object_sketch,
            "integrity_report": integrity_report,
            "elapsed_seconds": time.time() - started_at,
        }

    def run_er_test_st2(self) -> dict[str, Any]:
        started_at = time.time()

        step_started_at = time.time()
        stage1_object_sketch = self.extract_er_object_sketch()
        write_json(self.log_dir / "er_object_sketch.json", stage1_object_sketch)
        emit_step_done_log(
            prefix="NL2ER",
            step="extract_er_object_sketch",
            elapsed_seconds=time.time() - step_started_at,
            entity_count=len(stage1_object_sketch.get("entities") or []),
            relation_count=len(stage1_object_sketch.get("relations") or []),
        )

        step_started_at = time.time()
        stage1_integrity_report = self.validate_er_object_sketch_integrity(stage1_object_sketch)
        write_json(self.log_dir / "er_object_sketch_integrity.json", stage1_integrity_report)
        emit_step_done_log(
            prefix="NL2ER",
            step="validate_er_object_sketch_integrity",
            elapsed_seconds=time.time() - step_started_at,
            ok=stage1_integrity_report["passed"],
            passed=stage1_integrity_report["passed"],
            error_count=len(stage1_integrity_report["errors"]),
            warning_count=len(stage1_integrity_report["warnings"]),
        )

        step_started_at = time.time()
        semantic_review = self.review_er_object_sketch(stage1_object_sketch)
        write_json(self.log_dir / "er_semantic_review.json", semantic_review)
        emit_step_done_log(
            prefix="NL2ER",
            step="review_er_object_sketch",
            elapsed_seconds=time.time() - step_started_at,
            entity_count=len(semantic_review.get("entities") or []),
            relation_count=len(semantic_review.get("relations") or []),
            condition_count=len(semantic_review.get("conditions") or []),
        )

        step_started_at = time.time()
        integrity_report = self.validate_er_semantic_review_integrity(semantic_review)
        write_json(self.log_dir / "er_semantic_review_integrity.json", integrity_report)
        emit_step_done_log(
            prefix="NL2ER",
            step="validate_er_semantic_review_integrity",
            elapsed_seconds=time.time() - step_started_at,
            ok=integrity_report["passed"],
            passed=integrity_report["passed"],
            error_count=len(integrity_report["errors"]),
            warning_count=len(integrity_report["warnings"]),
        )

        return {
            "stage1_object_sketch": stage1_object_sketch,
            "stage1_integrity_report": stage1_integrity_report,
            "result": semantic_review,
            "integrity_report": integrity_report,
            "elapsed_seconds": time.time() - started_at,
        }

    def run(self) -> dict[str, Any]:
        payload = self.run_er_test_st2()
        result = {
            "entities": list(payload["result"].get("entities") or []),
            "relations": list(payload["result"].get("relations") or []),
            "conditions": list(payload["result"].get("conditions") or []),
        }
        if not payload["integrity_report"]["passed"]:
            raise ERSkeletonIntegrityError(
                integrity_report=payload["integrity_report"],
                result=result,
            )
        return {
            "stage1_object_sketch": payload["stage1_object_sketch"],
            "stage1_integrity_report": payload["stage1_integrity_report"],
            "result": result,
            "integrity_report": payload["integrity_report"],
            "elapsed_seconds": payload["elapsed_seconds"],
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run NL2ER from a parameterized JSON input.")
    parser.add_argument(
        "--mode",
        choices=["pipeline", "er_test_st1", "er_test_st2"],
        default="pipeline",
        help="`pipeline` runs the current stage1/stage2 ER workflow and writes `nl2er_output` using the current st2 final schema; `er_test_st1` runs only the stage1 object sketch prompt; `er_test_st2` runs stage1 object sketch extraction plus stage2 ER review.",
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--stage2-attribute-mode",
        choices=[ATTRIBUTE_MODE_LIST, ATTRIBUTE_MODE_DICT],
        default=ATTRIBUTE_MODE_LIST,
        help="Stage2 ER semantic review output mode for `attributes`: `list` keeps `[{'name', 'semantics'}]`, `dict` converts them to `{attr_name: semantics}`.",
    )
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_payload = read_input_payload(args.input_path, question_id=args.question_id)
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
        default_filename=(
            DEFAULT_OUTPUT_FILENAME
            if args.mode == "pipeline"
            else (
                ER_TEST_ST1_OUTPUT_FILENAME
                if args.mode == "er_test_st1"
                else ER_TEST_ST2_OUTPUT_FILENAME
            )
        ),
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
    print(f"[NL2ER] mode={args.mode}")
    print(f"[NL2ER] stage2_attribute_mode={args.stage2_attribute_mode}")

    nl2er = NL2ER(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=input_payload,
        model_config=args.model_config,
        stage2_attribute_mode=args.stage2_attribute_mode,
    )

    if args.mode == "er_test_st1":
        payload = nl2er.run_er_test_st1()
        write_json(
            output_path,
            build_er_test_st1_output_payload(
                object_sketch=payload["result"],
                integrity_report=payload["integrity_report"],
            ),
        )

        print(f"[NL2ER] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
        print(f"[NL2ER] output wrote to {output_path}")
        print(f"[NL2ER] logs wrote to {log_dir}")
        print(f"[NL2ER] metadata wrote to {metadata_dir}")

        if not payload["integrity_report"]["passed"]:
            print(f"[NL2ER] object sketch integrity check failed.")
            raise SystemExit(2)
        return

    if args.mode == "er_test_st2":
        payload = nl2er.run_er_test_st2()
        write_json(
            output_path,
            build_er_test_st2_output_payload(
                stage1_object_sketch=payload["stage1_object_sketch"],
                stage1_integrity_report=payload["stage1_integrity_report"],
                semantic_review=payload["result"],
                integrity_report=payload["integrity_report"],
            ),
        )

        print(f"[NL2ER] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
        print(f"[NL2ER] output wrote to {output_path}")
        print(f"[NL2ER] logs wrote to {log_dir}")
        print(f"[NL2ER] metadata wrote to {metadata_dir}")

        if not payload["integrity_report"]["passed"]:
            print(f"[NL2ER] semantic review integrity check failed.")
            raise SystemExit(2)
        return

    started_at = time.time()
    try:
        payload = nl2er.run()
    except ERSkeletonIntegrityError as exc:
        elapsed_seconds = time.time() - started_at
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
