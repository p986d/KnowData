import argparse
import json
import re
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.prompt.prompt_builder import PromptBuilder
from src.utils.run_log import build_timestamp, emit_step_done_log, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROMPT_DIR = PROJECT_ROOT / "src/prompt/prompt_template"
DEFAULT_LOG_ROOT = PROJECT_ROOT / "log/nl2er_hypothesis_diff"
DEFAULT_INPUT_FILENAME = "nl2er_output.json"
DEFAULT_OUTPUT_FILENAME = "nl2er_output_diff.json"
DEFAULT_BATCH_SUMMARY_FILENAME = "nl2er_diff_batch_summary.json"
TEMPLATE_KEY = "nl2er_diff_ideas"
TEMPLATE_NAME = "NL2ER_Diff_Ideas_v1.0.md"
SIDECAR_INPUT_FILENAMES = ("nl2er_input.json", "input.json", "run_context.json")
SIDECAR_SCHEMA_FILENAMES = ("schema_linking.json", "linked_table_profile.json")
CORE_ER_KEYS = ("entities", "relations", "connections", "conditions", "operations", "resolve_process")
ALLOWED_DIFF_TYPES = {
    "split_entity",
    "merge_entities",
    "reify_relation",
    "collapse_relation",
    "role_specialization",
    "condition_relocation",
    "aggregation_unit",
    "temporal_or_spatial_scope",
    "schema_grounding_alternative",
    "other",
}

JSON_FENCE_RE = re.compile(r"```json\s*(.*?)\s*```", flags=re.IGNORECASE | re.DOTALL)
SAFE_PATH_NAME_RE = re.compile(r"[^\w.-]+", flags=re.UNICODE)


@dataclass(slots=True)
class CaseInput:
    input_path: Path
    case_dir: Path
    relative_case_dir: Path


def resolve_path(path: str | Path) -> Path:
    resolved = Path(path).expanduser()
    if resolved.is_absolute():
        return resolved.resolve()
    return (Path.cwd() / resolved).resolve()


def read_json_object(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {file_path}, got {type(payload).__name__}.")
    return payload


def try_read_json_object(path: str | Path) -> dict[str, Any]:
    try:
        return read_json_object(path)
    except Exception:
        return {}


def json_extract(raw_text: str) -> str:
    matches = JSON_FENCE_RE.findall(raw_text or "")
    candidate = matches[-1] if matches else raw_text
    return str(candidate or "").strip()


def parse_json_object(raw_text: str) -> dict[str, Any]:
    extracted = json_extract(raw_text)
    try:
        payload = json.loads(extracted)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Failed to parse JSON response: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object response, got {type(payload).__name__}.")
    return payload


def unique_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        raw_items = [values]
    elif isinstance(values, list):
        raw_items = values
    else:
        return []

    output: list[str] = []
    seen: set[str] = set()
    for item in raw_items:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def normalize_attr_name(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(
            item.get("name")
            or item.get("attr")
            or item.get("attribute_name")
            or item.get("field_name")
            or ""
        ).strip()
    return ""


def attribute_names(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return unique_strings([normalize_attr_name(item) for item in value])


def entity_name(entity: dict[str, Any]) -> str:
    return str(entity.get("entity_name") or entity.get("name") or "").strip()


def relation_name(relation: dict[str, Any], *, kind: str = "relation") -> str:
    primary_key = "connection_name" if kind == "connection" else "relation_name"
    return str(relation.get(primary_key) or relation.get("name") or "").strip()


def participant_entity(participant: dict[str, Any]) -> str:
    return str(participant.get("entity") or participant.get("entity_name") or "").strip()


def participant_role(participant: dict[str, Any], *, fallback: str) -> str:
    return str(participant.get("role") or fallback).strip()


def participant_anchors(participant: dict[str, Any]) -> list[str]:
    return unique_strings(
        participant.get("anchor_attribute")
        or participant.get("anchor_attributes")
        or participant.get("identifier_attrs")
        or participant.get("primary_key")
    )


def normalize_er_core(er_payload: dict[str, Any]) -> dict[str, Any]:
    core: dict[str, Any] = {}
    for key in CORE_ER_KEYS:
        value = er_payload.get(key)
        if isinstance(value, list):
            core[key] = deepcopy(value)
        elif key in er_payload:
            core[key] = value
        elif key in {"entities", "relations", "conditions"}:
            core[key] = []
    return core


def empty_nl2er_output_diff_payload() -> dict[str, Any]:
    return {
        "entities": [],
        "relations": [],
        "conditions": [],
    }


def normalize_diff_string_list(value: Any) -> list[str]:
    if isinstance(value, dict):
        return unique_strings([normalize_attr_name(value)])
    if isinstance(value, list):
        return unique_strings(
            [
                normalize_attr_name(item) if isinstance(item, dict) else item
                for item in value
            ]
        )
    return unique_strings(value)


def normalize_diff_attributes(value: Any) -> list[dict[str, str]]:
    if value in (None, "", [], {}):
        return []

    if isinstance(value, dict):
        if any(key in value for key in ("name", "attr", "attribute_name", "field_name")):
            raw_items: list[Any] = [value]
        else:
            raw_items = [
                {"name": raw_name, "semantics": raw_semantics}
                for raw_name, raw_semantics in value.items()
            ]
    elif isinstance(value, list):
        raw_items = value
    else:
        raw_items = [value]

    attributes: list[dict[str, str]] = []
    for item in raw_items:
        name = ""
        semantics = ""
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = normalize_attr_name(item)
            for semantic_key in ("semantics", "semantic", "desc", "description", "meaning"):
                semantic_value = item.get(semantic_key)
                if semantic_value in (None, ""):
                    continue
                semantics = str(semantic_value).strip()
                break
            if not name and len(item) == 1:
                raw_name, raw_semantics = next(iter(item.items()))
                name = str(raw_name or "").strip()
                semantics = "" if raw_semantics is None else str(raw_semantics).strip()
        elif item is not None:
            name = str(item).strip()

        if not name and not semantics:
            continue
        attributes.append({"name": name, "semantics": semantics})
    return attributes


def normalize_diff_participants(value: Any) -> list[dict[str, Any]]:
    if value in (None, "", [], {}):
        return []
    raw_participants = value if isinstance(value, list) else [value]

    participants: list[dict[str, Any]] = []
    for item in raw_participants:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "").strip()
        entity = str(item.get("entity") or item.get("entity_name") or "").strip()
        anchor_attribute = normalize_diff_string_list(
            item.get("anchor_attribute")
            or item.get("anchor_attributes")
            or item.get("identifier_attrs")
            or item.get("primary_key")
        )
        if not role and not entity and not anchor_attribute:
            continue
        participants.append(
            {
                "role": role,
                "entity": entity,
                "anchor_attribute": anchor_attribute,
            }
        )
    return participants


def normalize_diff_entity_unit(unit: dict[str, Any]) -> dict[str, Any] | None:
    entity = {
        "entity_name": str(unit.get("entity_name") or unit.get("name") or "").strip(),
        "desc": str(unit.get("desc") or unit.get("description") or "").strip(),
        "grain": str(unit.get("grain") or "").strip(),
        "attributes": normalize_diff_attributes(unit.get("attributes") or unit.get("attrs")),
        "primary_key": normalize_diff_string_list(
            unit.get("primary_key") or unit.get("identifier_attrs") or unit.get("key_attributes")
        ),
    }
    if (
        not entity["entity_name"]
        and not entity["desc"]
        and not entity["grain"]
        and not entity["attributes"]
        and not entity["primary_key"]
    ):
        return None
    return entity


def normalize_diff_relation_unit(unit: dict[str, Any]) -> dict[str, Any] | None:
    relation = {
        "relation_name": str(unit.get("relation_name") or unit.get("name") or "").strip(),
        "desc": str(unit.get("desc") or unit.get("description") or "").strip(),
        "grain": str(unit.get("grain") or "").strip(),
        "participants": normalize_diff_participants(unit.get("participants")),
        "link_condition": str(unit.get("link_condition") or "").strip(),
        "attributes": normalize_diff_attributes(unit.get("attributes") or unit.get("attrs")),
    }
    if (
        not relation["relation_name"]
        and not relation["desc"]
        and not relation["grain"]
        and not relation["participants"]
        and not relation["link_condition"]
        and not relation["attributes"]
    ):
        return None
    return relation


def merge_diff_attributes(
    left: list[dict[str, str]],
    right: list[dict[str, str]],
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    by_name: dict[str, dict[str, str]] = {}
    for item in [*left, *right]:
        name = str(item.get("name") or "").strip()
        semantics = str(item.get("semantics") or "").strip()
        key = name.casefold()
        if key and key in by_name:
            if semantics and not by_name[key].get("semantics"):
                by_name[key]["semantics"] = semantics
            continue
        payload = {"name": name, "semantics": semantics}
        merged.append(payload)
        if key:
            by_name[key] = payload
    return merged


def merge_diff_string_values(left: list[str], right: list[str]) -> list[str]:
    return unique_strings([*left, *right])


def merge_diff_participants(
    left: list[dict[str, Any]],
    right: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    merged = deepcopy(left)
    by_entity: dict[str, dict[str, Any]] = {
        str(item.get("entity") or "").strip().casefold(): item
        for item in merged
        if str(item.get("entity") or "").strip()
    }
    for participant in right:
        entity = str(participant.get("entity") or "").strip()
        key = entity.casefold()
        if key and key in by_entity:
            target = by_entity[key]
            if participant.get("role") and not target.get("role"):
                target["role"] = participant["role"]
            target["anchor_attribute"] = merge_diff_string_values(
                list(target.get("anchor_attribute") or []),
                list(participant.get("anchor_attribute") or []),
            )
            continue
        merged.append(deepcopy(participant))
        if key:
            by_entity[key] = merged[-1]
    return merged


def merge_diff_entity_units(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(left)
    if not merged.get("desc") and right.get("desc"):
        merged["desc"] = right["desc"]
    if not merged.get("grain") and right.get("grain"):
        merged["grain"] = right["grain"]
    merged["attributes"] = merge_diff_attributes(
        list(merged.get("attributes") or []),
        list(right.get("attributes") or []),
    )
    merged["primary_key"] = merge_diff_string_values(
        list(merged.get("primary_key") or []),
        list(right.get("primary_key") or []),
    )
    return merged


def merge_diff_relation_units(
    left: dict[str, Any],
    right: dict[str, Any],
) -> dict[str, Any]:
    merged = deepcopy(left)
    if not merged.get("desc") and right.get("desc"):
        merged["desc"] = right["desc"]
    if not merged.get("grain") and right.get("grain"):
        merged["grain"] = right["grain"]
    if not merged.get("link_condition") and right.get("link_condition"):
        merged["link_condition"] = right["link_condition"]
    merged["participants"] = merge_diff_participants(
        list(merged.get("participants") or []),
        list(right.get("participants") or []),
    )
    merged["attributes"] = merge_diff_attributes(
        list(merged.get("attributes") or []),
        list(right.get("attributes") or []),
    )
    return merged


def relation_merge_key(relation: dict[str, Any]) -> tuple[str, tuple[str, ...]]:
    participant_entities = sorted(
        {
            str(participant.get("entity") or "").strip().casefold()
            for participant in list(relation.get("participants") or [])
            if str(participant.get("entity") or "").strip()
        }
    )
    return (
        str(relation.get("relation_name") or "").strip().casefold(),
        tuple(participant_entities),
    )


def merge_diff_output_units(output: dict[str, Any]) -> dict[str, Any]:
    merged_output = empty_nl2er_output_diff_payload()

    entity_index: dict[str, int] = {}
    for entity in list(output.get("entities") or []):
        key = str(entity.get("entity_name") or "").strip().casefold()
        if key and key in entity_index:
            index = entity_index[key]
            merged_output["entities"][index] = merge_diff_entity_units(
                merged_output["entities"][index],
                entity,
            )
            continue
        if key:
            entity_index[key] = len(merged_output["entities"])
        merged_output["entities"].append(deepcopy(entity))

    relation_index: dict[tuple[str, tuple[str, ...]], int] = {}
    for relation in list(output.get("relations") or []):
        key = relation_merge_key(relation)
        if key[0] and key in relation_index:
            index = relation_index[key]
            merged_output["relations"][index] = merge_diff_relation_units(
                merged_output["relations"][index],
                relation,
            )
            continue
        relation_index[key] = len(merged_output["relations"])
        merged_output["relations"].append(deepcopy(relation))

    merged_output["conditions"] = list(output.get("conditions") or [])
    return merged_output


def normalize_diff_condition_unit(unit: dict[str, Any]) -> dict[str, Any] | None:
    condition = {
        "condition_name": str(unit.get("condition_name") or unit.get("name") or "").strip(),
        "condition_type": str(unit.get("condition_type") or "").strip(),
        "targets": normalize_diff_string_list(unit.get("targets") or unit.get("target")),
        "description": str(unit.get("description") or unit.get("desc") or "").strip(),
    }
    if (
        not condition["condition_name"]
        and not condition["condition_type"]
        and not condition["targets"]
        and not condition["description"]
    ):
        return None
    return condition


def iter_diff_semantic_units(payload: dict[str, Any]):
    raw_diff_groups = payload.get("er_model_diff") or payload.get("er_model_diffs")
    if isinstance(raw_diff_groups, dict):
        diff_groups = [raw_diff_groups]
    elif isinstance(raw_diff_groups, list):
        diff_groups = list(raw_diff_groups)
    else:
        diff_groups = []

    top_level_units = payload.get("diff_semantic_unit") or payload.get("diff_semantic_units")
    if top_level_units:
        diff_groups.append({"diff_semantic_unit": top_level_units})

    for diff_group in diff_groups:
        if not isinstance(diff_group, dict):
            continue
        raw_units = diff_group.get("diff_semantic_unit")
        if raw_units is None:
            raw_units = diff_group.get("diff_semantic_units")
        if isinstance(raw_units, dict):
            units = [raw_units]
        elif isinstance(raw_units, list):
            units = raw_units
        else:
            units = []
        for unit in units:
            if isinstance(unit, dict):
                yield unit


def build_nl2er_output_diff_payload(payload: dict[str, Any]) -> dict[str, Any]:
    output = empty_nl2er_output_diff_payload()
    for unit in iter_diff_semantic_units(payload):
        if "entity_name" in unit:
            entity = normalize_diff_entity_unit(unit)
            if entity is not None:
                output["entities"].append(entity)
            continue
        if "relation_name" in unit:
            relation = normalize_diff_relation_unit(unit)
            if relation is not None:
                output["relations"].append(relation)
            continue
        if "name" in unit and "participants" in unit:
            relation = normalize_diff_relation_unit(unit)
            if relation is not None:
                output["relations"].append(relation)
            continue
        if "name" in unit:
            entity = normalize_diff_entity_unit(unit)
            if entity is not None:
                output["entities"].append(entity)
            continue
        if "condition_name" in unit:
            condition = normalize_diff_condition_unit(unit)
            if condition is not None:
                output["conditions"].append(condition)
            continue
        if "primary_key" in unit and "participants" not in unit:
            entity = normalize_diff_entity_unit(unit)
            if entity is not None:
                output["entities"].append(entity)
    return merge_diff_output_units(output)


def diff_output_warnings(diff_output: dict[str, Any]) -> list[str]:
    has_units = any(diff_output.get(key) for key in ("entities", "relations", "conditions"))
    if has_units:
        return []
    return ["No valid er_model_diff.diff_semantic_unit items were produced."]


def extract_context_value(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def read_sidecar_context(input_path: str | Path) -> dict[str, str]:
    input_file = Path(input_path)
    case_dir = input_file if input_file.is_dir() else input_file.parent
    context = {
        "question_id": "",
        "db_id": "",
        "user_intent": "",
        "db_hint": "",
        "external_knowledge": "",
    }

    for filename in SIDECAR_INPUT_FILENAMES:
        payload = try_read_json_object(case_dir / filename)
        if not payload:
            continue
        if not context["question_id"]:
            context["question_id"] = extract_context_value(payload, "question_id", "instance_id")
        if not context["db_id"]:
            context["db_id"] = extract_context_value(payload, "db_id", "database_id")
        if not context["user_intent"]:
            context["user_intent"] = extract_context_value(
                payload,
                "user_intent",
                "instruction",
                "question",
            )
        if not context["db_hint"]:
            context["db_hint"] = extract_context_value(payload, "db_hint", "schema_hint")
        if not context["external_knowledge"]:
            context["external_knowledge"] = extract_context_value(
                payload,
                "external_knowledge",
                "evidence",
                "knowledge",
            )

    return context


def collect_linked_names(payload: dict[str, Any]) -> dict[str, Any]:
    table_set: set[str] = set()
    column_set: set[str] = set()
    unit_summaries: list[dict[str, Any]] = []

    def add_payload(unit_name: str, unit_payload: Any) -> None:
        if not isinstance(unit_payload, dict):
            return
        tables = unique_strings(unit_payload.get("linked_tables") or unit_payload.get("tables"))
        columns = unique_strings(unit_payload.get("linked_columns") or unit_payload.get("columns"))
        table_set.update(tables)
        column_set.update(columns)
        if unit_name or tables or columns:
            unit_summaries.append(
                {
                    "unit_name": unit_name,
                    "linked_tables": tables,
                    "linked_columns": columns,
                }
            )

    if "linked_tables" in payload or "linked_columns" in payload:
        add_payload("question", payload)

    for key, value in payload.items():
        if key == "linked_table_profiles" and isinstance(value, list):
            for index, profile in enumerate(value):
                add_payload(f"linked_table_profile_{index + 1}", profile)
            continue
        add_payload(str(key), value)

    return {
        "linked_tables": sorted(table_set),
        "linked_columns": sorted(column_set),
        "units": unit_summaries,
    }


def read_schema_context(input_path: str | Path) -> dict[str, Any]:
    input_file = Path(input_path)
    case_dir = input_file if input_file.is_dir() else input_file.parent
    for filename in SIDECAR_SCHEMA_FILENAMES:
        candidate = case_dir / filename
        if not candidate.exists():
            continue
        payload = try_read_json_object(candidate)
        if not payload:
            continue
        return {
            "source_path": str(candidate),
            "summary": collect_linked_names(payload),
        }
    return {
        "source_path": "",
        "summary": {
            "linked_tables": [],
            "linked_columns": [],
            "units": [],
        },
    }


def read_schema_linking_payload(input_path: str | Path) -> dict[str, Any]:
    input_file = Path(input_path)
    case_dir = input_file if input_file.is_dir() else input_file.parent
    candidate = case_dir / "schema_linking.json"
    if not candidate.exists():
        return {}
    payload = try_read_json_object(candidate)
    if not payload:
        return {}
    return {
        "source_path": str(candidate),
        "payload": payload,
    }


def extract_resolve_process(source_payload: dict[str, Any]) -> list[Any]:
    value = source_payload.get("resolve_process")
    return value if isinstance(value, list) else []


def validate_source_er(er_payload: Any) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(er_payload, dict):
        return ["ER payload must be an object."], warnings

    entities = er_payload.get("entities")
    relations = er_payload.get("relations", [])
    connections = er_payload.get("connections", [])
    conditions = er_payload.get("conditions", [])

    if not isinstance(entities, list):
        errors.append("`entities` must be a list.")
        entities = []
    if not isinstance(relations, list):
        errors.append("`relations` must be a list when present.")
    if not isinstance(connections, list):
        errors.append("`connections` must be a list when present.")
    if not isinstance(conditions, list):
        errors.append("`conditions` must be a list when present.")
    if not entities:
        errors.append("At least one entity is required.")

    seen_entities: set[str] = set()
    for index, entity in enumerate(ensure_dict_list(entities)):
        name = entity_name(entity)
        if not name:
            errors.append(f"entities[{index}] is missing `entity_name` or `name`.")
            continue
        if name in seen_entities:
            errors.append(f"Duplicate entity name `{name}`.")
        seen_entities.add(name)

    return errors, warnings


def build_semantic_unit_inventory(er_payload: dict[str, Any]) -> list[dict[str, Any]]:
    units: list[dict[str, Any]] = []

    for index, entity in enumerate(ensure_dict_list(er_payload.get("entities"))):
        name = entity_name(entity) or f"entity_{index + 1}"
        units.append(
            {
                "unit_id": f"entity:{name}",
                "unit_type": "entity",
                "name": name,
                "desc": str(entity.get("desc") or entity.get("description") or "").strip(),
                "grain": str(entity.get("grain") or "").strip(),
                "attributes": attribute_names(entity.get("attributes") or entity.get("attrs")),
                "primary_key": unique_strings(entity.get("primary_key") or entity.get("identifier_attrs")),
            }
        )

    for unit_key, unit_type, name_key in (
        ("relations", "relation", "relation_name"),
        ("connections", "connection", "connection_name"),
    ):
        for index, relation in enumerate(ensure_dict_list(er_payload.get(unit_key))):
            name = relation_name(relation, kind=unit_type) or f"{unit_type}_{index + 1}"
            participants = []
            for participant_index, participant in enumerate(ensure_dict_list(relation.get("participants"))):
                participants.append(
                    {
                        "role": participant_role(participant, fallback=f"participant_{participant_index + 1}"),
                        "entity": participant_entity(participant),
                        "anchor_attribute": participant_anchors(participant),
                    }
                )
            units.append(
                {
                    "unit_id": f"{unit_type}:{name}",
                    "unit_type": unit_type,
                    "name": name,
                    "name_key": name_key,
                    "relation_type": str(relation.get("relation_type") or "").strip(),
                    "desc": str(relation.get("desc") or relation.get("description") or "").strip(),
                    "grain": str(relation.get("grain") or "").strip(),
                    "participants": participants,
                    "link_condition": str(
                        relation.get("link_condition") or relation.get("match_rule") or ""
                    ).strip(),
                    "attributes": attribute_names(relation.get("attributes") or relation.get("attrs")),
                }
            )

    for index, condition in enumerate(ensure_dict_list(er_payload.get("conditions"))):
        name = str(condition.get("condition_name") or condition.get("name") or f"condition_{index + 1}").strip()
        units.append(
            {
                "unit_id": f"condition:{name}",
                "unit_type": "condition",
                "name": name,
                "condition_type": str(condition.get("condition_type") or "").strip(),
                "targets": unique_strings(condition.get("targets") or condition.get("target")),
                "description": str(condition.get("description") or condition.get("desc") or "").strip(),
            }
        )

    return units


def validate_diff_ideas_payload(
    payload: Any,
    *,
    semantic_units: list[dict[str, Any]],
    requested_idea_count: int,
) -> tuple[list[str], list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    del semantic_units
    if not isinstance(payload, dict):
        return [], ["Diff ideas payload must be an object."], warnings
    raw_ideas = payload.get("diff_ideas") or payload.get("ideas")
    if not isinstance(raw_ideas, list):
        return [], ["Diff ideas payload must contain a `diff_ideas` list."], warnings

    ideas: list[str] = []
    seen: set[str] = set()
    for index, raw_idea in enumerate(raw_ideas):
        if not isinstance(raw_idea, str):
            errors.append(f"diff_ideas[{index}] must be a natural-language string.")
            continue
        text = " ".join(raw_idea.split())
        if not text:
            errors.append(f"diff_ideas[{index}] must not be empty.")
            continue
        if text in seen:
            warnings.append(f"Duplicate diff idea ignored at index {index}.")
            continue
        ideas.append(text)
        seen.add(text)

    if not ideas:
        errors.append("No valid diff ideas were produced.")
    elif len(ideas) < requested_idea_count:
        warnings.append(
            f"Produced {len(ideas)} valid ideas, fewer than requested {requested_idea_count}."
        )

    return ideas, errors, warnings


def first_unit_name(units: list[dict[str, Any]], unit_type: str) -> str:
    for unit in units:
        if unit.get("unit_type") == unit_type and unit.get("name"):
            return str(unit["name"])
    return ""


def find_start_end_relations(units: list[dict[str, Any]]) -> tuple[str, str] | None:
    start_name = ""
    end_name = ""
    for unit in units:
        if unit.get("unit_type") != "relation":
            continue
        text = f"{unit.get('name', '')} {unit.get('desc', '')}".casefold()
        if not start_name and "start" in text:
            start_name = str(unit.get("name") or "")
        if not end_name and "end" in text:
            end_name = str(unit.get("name") or "")
    if start_name and end_name and start_name != end_name:
        return start_name, end_name
    return None


def build_dry_run_payload(
    *,
    source_er: dict[str, Any],
    semantic_units: list[dict[str, Any]],
    idea_count: int,
) -> dict[str, Any]:
    ideas: list[str] = []

    first_relation = first_unit_name(semantic_units, "relation")
    if first_relation:
        ideas.append(
            f"关系 `{first_relation}` 可以不只作为普通实体间关系实现，也可以实体化为一个具有独立粒度的事实/事件语义单元；"
            "这种设计会把关系实例本身变成可挂载属性和可被后续推理引用的对象，同时保持原参与实体、连接条件和筛选语义不变。"
        )

    first_entity = first_unit_name(semantic_units, "entity")
    if first_entity:
        ideas.append(
            f"实体 `{first_entity}` 的属性可以有另一种设计：保留一个表示对象身份和基础粒度的核心实体，"
            "将描述性、度量性或实现细节属性拆到关联的明细语义单元中；这种设计不改变对象含义，但会改变属性挂载位置和后续关系锚点设计。"
        )

    start_end = find_start_end_relations(semantic_units)
    if start_end is not None:
        start_relation, end_relation = start_end
        ideas.append(
            f"`{start_relation}` 与 `{end_relation}` 这类成对角色关系可以有两种合理设计："
            "一种是保留两个独立基础关系以强调起点/终点语义差异，另一种是抽象为一个带 start/end 角色的统一关系模式；"
            "后一种设计要求显式保留角色，避免在查询推理中混淆起止位置。"
        )

    condition_name = first_unit_name(semantic_units, "condition")
    if condition_name:
        ideas.append(
            f"条件 `{condition_name}` 可以作为全局约束保留，也可以下沉到其约束属性所在的实体或关系语义中；"
            "两种设计都合理，但后者会让筛选语义更贴近被约束单元，生成完整 ER 时必须确保筛选范围、常量值和时间/空间口径不被弱化。"
        )

    ideas.append(
        "如果问题包含分组统计、平均值、计数、排序或派生指标，最终分析结果可以不只放在 resolve_process 中，"
        "也可以设计为一个派生结果语义单元，显式承载分组键和度量属性；这种设计会影响 ER 到查询生成阶段的目标粒度表达，"
        "但不应把派生结果误当成支撑查询的基础实体。"
    )

    selected_ideas = ideas[:idea_count]
    source_diff_units: list[dict[str, Any]] = [
        deepcopy(item) for item in ensure_dict_list(source_er.get("relations"))
    ]
    source_diff_units.extend(
        deepcopy(item) for item in ensure_dict_list(source_er.get("entities"))
    )
    er_model_diff: list[dict[str, Any]] = []
    for index, idea in enumerate(selected_ideas):
        diff_semantic_unit = []
        if source_diff_units:
            diff_semantic_unit = [deepcopy(source_diff_units[index % len(source_diff_units)])]
        er_model_diff.append(
            {
                "diff_idea": idea,
                "diff_semantic_unit": diff_semantic_unit,
            }
        )

    return {
        "construction_status": "dry_run",
        "diff_ideas": selected_ideas,
        "er_model_diff": er_model_diff,
        "selection_notes": [
            "Dry-run generated deterministic idea descriptions without calling the LLM.",
        ],
    }


def format_idea_summary(payload: dict[str, Any]) -> str:
    ideas = [item for item in payload.get("diff_ideas") or [] if isinstance(item, str)]
    if not ideas:
        return "[NL2ER_DIFF_IDEAS] no diff ideas"

    lines = [f"[NL2ER_DIFF_IDEAS] ideas={len(ideas)}"]
    for index, idea in enumerate(ideas, start=1):
        lines.append(f"{index}. {idea}")
    return "\n".join(lines)


class NL2ERHypothesisDiffConstructor:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        template_name: str = TEMPLATE_NAME,
        max_retry: int = 1,
        dry_run: bool = False,
    ) -> None:
        if reasoning_mode is not None and reasoning_mode not in REASONING_MODE_MAP:
            valid_modes = ", ".join(sorted(REASONING_MODE_MAP))
            raise ValueError(
                f"Unsupported reasoning mode `{reasoning_mode}`. Expected one of: {valid_modes}."
            )

        self.prompt_dir = resolve_path(prompt_dir)
        self.log_dir = resolve_path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.model_config = model_config
        self.reasoning_mode = reasoning_mode
        self.template_name = template_name
        self.max_retry = max(0, int(max_retry))
        self.dry_run = dry_run

        self.llm: LLMClient | None = None
        self.model_config_name = model_config or ""
        if not dry_run:
            settings = load_settings()
            self.model_config_name = model_config or settings.llm.default_model
            llm_config = apply_reasoning_mode(
                settings.llm.get(self.model_config_name),
                self.reasoning_mode,
            )
            self.llm = LLMClient(llm_config)

        self.prompt_builder = PromptBuilder(template_dir=self.prompt_dir, strict_undefined=True)
        self.prompt_builder.register_template(
            name=TEMPLATE_KEY,
            template_name=self.template_name,
            required_vars=[
                "idea_count",
                "user_intent",
                "semantic_units",
                "resolve_process",
                "schema_linking",
                "previous_response",
                "validation_errors",
            ],
            default_vars={
                "question_id": "",
                "db_id": "",
                "db_hint": "",
                "external_knowledge": "",
                "strategy_focus": "",
            },
            description="Construct natural-language NL2ER diff ideas for later candidate ER expansion.",
        )

    def build_prompt(
        self,
        *,
        idea_count: int,
        context: dict[str, str],
        source_er: dict[str, Any],
        semantic_units: list[dict[str, Any]],
        resolve_process: list[Any],
        schema_linking: dict[str, Any],
        strategy_focus: str = "",
        previous_response: str = "",
        validation_errors: list[str] | None = None,
    ) -> str:
        return self.prompt_builder.build_text(
            TEMPLATE_KEY,
            vars={
                "idea_count": idea_count,
                "question_id": context.get("question_id", ""),
                "db_id": context.get("db_id", ""),
                "user_intent": context.get("user_intent", ""),
                "db_hint": context.get("db_hint", ""),
                "external_knowledge": context.get("external_knowledge", ""),
                "semantic_units": semantic_units,
                "resolve_process": resolve_process,
                "schema_linking": schema_linking,
                "strategy_focus": strategy_focus,
                "previous_response": previous_response,
                "validation_errors": validation_errors or [],
            },
        )

    def run_case(
        self,
        *,
        input_path: str | Path,
        output_path: str | Path,
        idea_count: int,
        user_intent: str | None = None,
        question_id: str | None = None,
        db_id: str | None = None,
        db_hint: str = "",
        external_knowledge: str = "",
        strategy_focus: str = "",
        include_resolve_process: bool = False,
        include_schema_linking: bool = False,
    ) -> dict[str, Any]:
        started_at = time.perf_counter()
        input_path = resolve_path(input_path)
        output_path = resolve_path(output_path)
        source_payload = read_json_object(input_path)
        source_er = normalize_er_core(source_payload)
        source_errors, source_warnings = validate_source_er(source_er)

        context = read_sidecar_context(input_path)
        if question_id:
            context["question_id"] = question_id
        if db_id:
            context["db_id"] = db_id
        if user_intent:
            context["user_intent"] = user_intent
        if db_hint:
            context["db_hint"] = db_hint
        if external_knowledge:
            context["external_knowledge"] = external_knowledge
        if not context["question_id"]:
            context["question_id"] = input_path.parent.name

        semantic_units = build_semantic_unit_inventory(source_er)
        resolve_process = extract_resolve_process(source_payload) if include_resolve_process else []
        schema_linking = read_schema_linking_payload(input_path) if include_schema_linking else {}

        attempts: list[dict[str, Any]] = []
        best_ideas: list[str] = []
        best_selection_notes: list[str] = []
        best_warnings: list[str] = []
        best_diff_output = empty_nl2er_output_diff_payload()
        validation_errors: list[str] = list(source_errors)
        previous_response = ""

        if source_errors:
            output_payload = {
                "ok": False,
                "construction_status": "error",
                "status": "error",
                "question_id": context.get("question_id", ""),
                "db_id": context.get("db_id", ""),
                "input_path": str(input_path),
                "output_path": str(output_path),
                "log_dir": str(self.log_dir),
                "model_config": self.model_config_name,
                "reasoning_mode": self.reasoning_mode,
                "idea_count_requested": idea_count,
                "context": context,
                "semantic_units": semantic_units,
                "diff_ideas": [],
                "diff_output": empty_nl2er_output_diff_payload(),
                "selection_notes": [],
                "warnings": source_warnings,
                "errors": source_errors,
                "attempts": attempts,
                "elapsed_seconds": time.perf_counter() - started_at,
            }
            write_json(output_path, empty_nl2er_output_diff_payload())
            return output_payload

        if self.dry_run:
            prompt = self.build_prompt(
                idea_count=idea_count,
                context=context,
                source_er=source_er,
                semantic_units=semantic_units,
                resolve_process=resolve_process,
                schema_linking=schema_linking,
                strategy_focus=strategy_focus,
                previous_response="",
                validation_errors=[],
            )
            (self.log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
            raw_payload = build_dry_run_payload(
                source_er=source_er,
                semantic_units=semantic_units,
                idea_count=idea_count,
            )
            (self.log_dir / "response.md").write_text(
                json.dumps(raw_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            ideas, errors, warnings = validate_diff_ideas_payload(
                raw_payload,
                semantic_units=semantic_units,
                requested_idea_count=idea_count,
            )
            candidate_diff_output = build_nl2er_output_diff_payload(raw_payload)
            warnings = warnings + diff_output_warnings(candidate_diff_output)
            attempts.append(
                {
                    "attempt": 1,
                    "ok": not errors and bool(ideas),
                    "dry_run": True,
                    "diff_entity_count": len(candidate_diff_output.get("entities") or []),
                    "diff_relation_count": len(candidate_diff_output.get("relations") or []),
                    "diff_condition_count": len(candidate_diff_output.get("conditions") or []),
                    "errors": errors,
                    "warnings": warnings,
                }
            )
            best_ideas = ideas
            best_diff_output = candidate_diff_output
            best_selection_notes = unique_strings(raw_payload.get("selection_notes"))
            validation_errors = errors
            best_warnings = warnings
        else:
            if self.llm is None:
                raise RuntimeError("LLM client is not initialized. Disable --dry-run to call the LLM.")
            for attempt in range(1, self.max_retry + 2):
                prompt = self.build_prompt(
                    idea_count=idea_count,
                    context=context,
                    source_er=source_er,
                    semantic_units=semantic_units,
                    resolve_process=resolve_process,
                    schema_linking=schema_linking,
                    strategy_focus=strategy_focus,
                    previous_response=previous_response,
                    validation_errors=validation_errors,
                )
                prompt_path = self.log_dir / "prompt.md"
                prompt_path.write_text(prompt, encoding="utf-8")

                raw_response = self.llm.single_turn(prompt)
                previous_response = raw_response or ""
                response_path = self.log_dir / "response.md"
                response_path.write_text(previous_response, encoding="utf-8")

                attempt_payload: dict[str, Any] = {
                    "attempt": attempt,
                    "prompt_path": str(prompt_path),
                    "response_path": str(response_path),
                    "raw_response": previous_response,
                    "ok": False,
                    "errors": [],
                    "warnings": [],
                }
                try:
                    parsed = parse_json_object(previous_response)
                    ideas, errors, warnings = validate_diff_ideas_payload(
                        parsed,
                        semantic_units=semantic_units,
                        requested_idea_count=idea_count,
                    )
                    candidate_diff_output = build_nl2er_output_diff_payload(parsed)
                    warnings = warnings + diff_output_warnings(candidate_diff_output)
                    attempt_payload["idea_count"] = len(ideas)
                    attempt_payload["diff_entity_count"] = len(
                        candidate_diff_output.get("entities") or []
                    )
                    attempt_payload["diff_relation_count"] = len(
                        candidate_diff_output.get("relations") or []
                    )
                    attempt_payload["diff_condition_count"] = len(
                        candidate_diff_output.get("conditions") or []
                    )
                    attempt_payload["errors"] = errors
                    attempt_payload["warnings"] = warnings
                    attempt_payload["ok"] = bool(ideas) and not errors
                    if ideas:
                        best_ideas = ideas
                        best_diff_output = candidate_diff_output
                        best_selection_notes = unique_strings(parsed.get("selection_notes"))
                        best_warnings = warnings
                    validation_errors = errors
                    if bool(ideas) and not errors:
                        attempts.append(attempt_payload)
                        break
                except Exception as exc:
                    validation_errors = [str(exc)]
                    attempt_payload["errors"] = validation_errors
                attempts.append(attempt_payload)

        status = "ok" if best_ideas and not validation_errors else "partial" if best_ideas else "error"
        if self.dry_run and status == "ok":
            status = "dry_run"
        output_payload = {
            "ok": bool(best_ideas),
            "construction_status": status,
            "status": status,
            "question_id": context.get("question_id", ""),
            "db_id": context.get("db_id", ""),
            "input_path": str(input_path),
            "output_path": str(output_path),
            "log_dir": str(self.log_dir),
            "model_config": self.model_config_name,
            "reasoning_mode": self.reasoning_mode,
            "idea_count_requested": idea_count,
            "context": context,
            "resolve_process": resolve_process,
            "schema_linking": schema_linking,
            "semantic_units": semantic_units,
            "diff_ideas": best_ideas,
            "diff_output": best_diff_output,
            "selection_notes": best_selection_notes,
            "warnings": source_warnings + best_warnings,
            "errors": validation_errors,
            "attempts": attempts,
            "elapsed_seconds": time.perf_counter() - started_at,
        }
        write_json(output_path, best_diff_output)
        return output_payload


NL2ERDiffConstructor = NL2ERHypothesisDiffConstructor


def resolve_input_file(path: str | Path) -> Path:
    resolved = resolve_path(path)
    if resolved.is_dir():
        candidate = resolved / DEFAULT_INPUT_FILENAME
        if not candidate.exists():
            raise FileNotFoundError(f"No `{DEFAULT_INPUT_FILENAME}` found under {resolved}.")
        return candidate
    return resolved


def discover_case_inputs(path: str | Path) -> tuple[Path, list[CaseInput]]:
    root = resolve_path(path)
    if not root.exists():
        raise FileNotFoundError(f"Input path does not exist: {root}")

    if root.is_file():
        return root.parent, [
            CaseInput(
                input_path=root,
                case_dir=root.parent,
                relative_case_dir=Path(root.parent.name),
            )
        ]

    direct_input = root / DEFAULT_INPUT_FILENAME
    if direct_input.exists():
        return root, [
            CaseInput(
                input_path=direct_input.resolve(),
                case_dir=root,
                relative_case_dir=Path(root.name),
            )
        ]

    input_paths = sorted({item.resolve() for item in root.rglob(DEFAULT_INPUT_FILENAME)})
    if not input_paths:
        raise FileNotFoundError(f"No `{DEFAULT_INPUT_FILENAME}` files were found under {root}.")

    return root, [
        CaseInput(
            input_path=input_path,
            case_dir=input_path.parent,
            relative_case_dir=input_path.parent.relative_to(root),
        )
        for input_path in input_paths
    ]


def safe_run_name(value: str) -> str:
    text = re.sub(r"_+", "_", SAFE_PATH_NAME_RE.sub("_", value)).strip("._-")
    return text or "run"


def resolve_log_dir(
    *,
    log_root: str | Path,
    input_path: str | Path,
    run_name: str = "",
    question_id: str = "",
) -> Path:
    root = resolve_path(log_root)
    if run_name.strip():
        name = safe_run_name(run_name)
    else:
        base = question_id.strip() or Path(input_path).parent.name or Path(input_path).stem
        name = f"{safe_run_name(base)}_{build_timestamp()}"
    log_dir = root / name
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"Expected a positive integer, got `{value}`.")
    return parsed


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Construct natural-language NL2ER diff ideas from one nl2er_output.json file "
            "or a metadata case directory."
        )
    )
    parser.add_argument(
        "input_path",
        nargs="?",
        default=None,
        help="Path to nl2er_output.json or to a metadata case directory containing it.",
    )
    parser.add_argument(
        "--input-path",
        dest="input_path_option",
        default=None,
        help="Explicit input path. Overrides the positional input_path when set.",
    )
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--output-filename", default=DEFAULT_OUTPUT_FILENAME)
    parser.add_argument("--batch-summary-filename", default=DEFAULT_BATCH_SUMMARY_FILENAME)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--run-name", default="")
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        "--reason-mode",
        dest="reasoning_mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument("--idea-count", type=positive_int, default=4)
    parser.add_argument("--max-batch-workers", type=positive_int, default=4)
    parser.add_argument(
        "--variant-count",
        type=positive_int,
        default=None,
        help="Deprecated alias for --idea-count.",
    )
    parser.add_argument("--max-retry", type=int, default=1)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--user-intent", default=None)
    parser.add_argument("--db-hint", default="")
    parser.add_argument("--external-knowledge", default="")
    parser.add_argument(
        "--include-resolve-process",
        action="store_true",
        help="Include nl2er_output.json resolve_process in the prompt.",
    )
    parser.add_argument(
        "--include-schema-linking",
        action="store_true",
        help="Include sibling schema_linking.json in the prompt.",
    )
    parser.add_argument(
        "--strategy-focus",
        default="",
        help="Optional comma-separated idea strategies to emphasize.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Generate deterministic local diff ideas and prompt logs without calling the LLM.",
    )
    parser.add_argument(
        "--no-print-ideas",
        action="store_true",
        help="Do not print the idea summary after the run.",
    )
    parser.add_argument("--print-output", action="store_true")
    return parser


def run_single_from_args(args: argparse.Namespace, case_input: CaseInput) -> tuple[int, dict[str, Any]]:
    idea_count = args.variant_count or args.idea_count
    input_path = case_input.input_path
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")

    sidecar_context = read_sidecar_context(input_path)
    resolved_question_id = str(args.question_id or sidecar_context.get("question_id") or "").strip()
    output_path = (
        resolve_path(args.output_path)
        if args.output_path is not None
        else input_path.parent / args.output_filename
    )
    log_dir = resolve_log_dir(
        log_root=args.log_root,
        input_path=input_path,
        run_name=args.run_name,
        question_id=resolved_question_id,
    )

    print(f"[NL2ER_DIFF] input_path={input_path}")
    print(f"[NL2ER_DIFF] output_path={output_path}")
    print(f"[NL2ER_DIFF] log_dir={log_dir}")
    print(f"[NL2ER_DIFF] dry_run={bool(args.dry_run)}")
    if args.reasoning_mode:
        print(f"[NL2ER_DIFF] reasoning_mode={args.reasoning_mode}")

    constructor = NL2ERHypothesisDiffConstructor(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        max_retry=args.max_retry,
        dry_run=bool(args.dry_run),
    )
    payload = constructor.run_case(
        input_path=input_path,
        output_path=output_path,
        idea_count=idea_count,
        question_id=args.question_id,
        db_id=args.db_id,
        user_intent=args.user_intent,
        db_hint=args.db_hint,
        external_knowledge=args.external_knowledge,
        strategy_focus=args.strategy_focus,
        include_resolve_process=bool(args.include_resolve_process),
        include_schema_linking=bool(args.include_schema_linking),
    )

    emit_step_done_log(
        prefix="NL2ER_DIFF",
        step="done",
        elapsed_seconds=float(payload.get("elapsed_seconds") or 0.0),
        ok=bool(payload.get("ok")),
        status=str(payload.get("status") or ""),
        ideas=len(payload.get("diff_ideas") or []),
    )
    if not args.no_print_ideas:
        print(format_idea_summary(payload))
    if args.print_output:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return (0 if payload.get("ok") else 1), payload


def run_batch_case(
    *,
    case_input: CaseInput,
    batch_root: Path,
    batch_log_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    started_at = time.perf_counter()
    idea_count = args.variant_count or args.idea_count
    output_path = case_input.case_dir / args.output_filename
    log_dir = batch_log_dir / case_input.relative_case_dir
    constructor = NL2ERHypothesisDiffConstructor(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        max_retry=args.max_retry,
        dry_run=bool(args.dry_run),
    )
    payload = constructor.run_case(
        input_path=case_input.input_path,
        output_path=output_path,
        idea_count=idea_count,
        question_id=None,
        db_id=args.db_id,
        user_intent=None,
        db_hint=args.db_hint,
        external_knowledge=args.external_knowledge,
        strategy_focus=args.strategy_focus,
        include_resolve_process=bool(args.include_resolve_process),
        include_schema_linking=bool(args.include_schema_linking),
    )
    summary = {
        "ok": bool(payload.get("ok")),
        "status": payload.get("status") or payload.get("construction_status") or "",
        "question_id": payload.get("question_id") or "",
        "db_id": payload.get("db_id") or "",
        "case_dir": str(case_input.case_dir),
        "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
        "input_path": str(case_input.input_path),
        "output_path": str(output_path),
        "log_dir": str(log_dir),
        "batch_root": str(batch_root),
        "idea_count": len(payload.get("diff_ideas") or []),
        "diff_entity_count": len((payload.get("diff_output") or {}).get("entities") or []),
        "diff_relation_count": len((payload.get("diff_output") or {}).get("relations") or []),
        "diff_condition_count": len((payload.get("diff_output") or {}).get("conditions") or []),
        "errors": payload.get("errors") or [],
        "warnings": payload.get("warnings") or [],
        "elapsed_seconds": round(time.perf_counter() - started_at, 3),
        "summary": payload,
    }
    write_json(log_dir / "case_summary.json", summary)
    return summary


def run_batch_case_safe(
    *,
    case_input: CaseInput,
    batch_root: Path,
    batch_log_dir: Path,
    args: argparse.Namespace,
) -> dict[str, Any]:
    output_path = case_input.case_dir / args.output_filename
    log_dir = batch_log_dir / case_input.relative_case_dir
    try:
        return run_batch_case(
            case_input=case_input,
            batch_root=batch_root,
            batch_log_dir=batch_log_dir,
            args=args,
        )
    except Exception as exc:
        context = read_sidecar_context(case_input.input_path)
        failure_payload = {
            "ok": False,
            "construction_status": "error",
            "status": "error",
            "question_id": context.get("question_id") or case_input.case_dir.name,
            "db_id": context.get("db_id") or args.db_id or "",
            "case_dir": str(case_input.case_dir),
            "relative_case_dir": str(case_input.relative_case_dir).replace("\\", "/"),
            "input_path": str(case_input.input_path),
            "output_path": str(output_path),
            "log_dir": str(log_dir),
            "batch_root": str(batch_root),
            "diff_ideas": [],
            "diff_output": empty_nl2er_output_diff_payload(),
            "selection_notes": [],
            "errors": [str(exc)],
            "traceback": traceback.format_exc(),
        }
        write_json(output_path, empty_nl2er_output_diff_payload())
        write_json(log_dir / "error.json", failure_payload)
        return failure_payload


def run_batch_from_args(
    args: argparse.Namespace,
    *,
    batch_root: Path,
    case_inputs: list[CaseInput],
) -> tuple[int, dict[str, Any]]:
    batch_started_at = time.perf_counter()
    run_name = safe_run_name(args.run_name) if str(args.run_name or "").strip() else build_timestamp()
    batch_log_dir = resolve_path(args.log_root) / run_name
    batch_log_dir.mkdir(parents=True, exist_ok=True)
    worker_count = max(1, min(args.max_batch_workers, len(case_inputs)))

    print(f"[NL2ER_DIFF_BATCH] batch_root={batch_root}")
    print(f"[NL2ER_DIFF_BATCH] discovered_cases={len(case_inputs)}")
    print(f"[NL2ER_DIFF_BATCH] log_dir={batch_log_dir}")
    print(f"[NL2ER_DIFF_BATCH] max_batch_workers={worker_count}")
    print(f"[NL2ER_DIFF_BATCH] dry_run={bool(args.dry_run)}")
    if args.reasoning_mode:
        print(f"[NL2ER_DIFF_BATCH] reasoning_mode={args.reasoning_mode}")

    summary_slots: list[dict[str, Any] | None] = [None] * len(case_inputs)
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        future_map = {
            executor.submit(
                run_batch_case_safe,
                case_input=case_input,
                batch_root=batch_root,
                batch_log_dir=batch_log_dir,
                args=args,
            ): index
            for index, case_input in enumerate(case_inputs)
        }
        for future in as_completed(future_map):
            index = future_map[future]
            summary = future.result()
            summary_slots[index] = summary
            completed = sum(1 for item in summary_slots if item is not None)
            print(
                "[NL2ER_DIFF_BATCH] progress "
                f"completed={completed}/{len(case_inputs)} "
                f"question_id={summary.get('question_id', '')} "
                f"ok={summary.get('ok', False)} "
                f"status={summary.get('status', '')}"
            )

    summaries = [item for item in summary_slots if item is not None]
    ok_cases = sum(1 for item in summaries if item.get("ok"))
    batch_payload = {
        "timestamp": run_name,
        "batch_root": str(batch_root),
        "log_dir": str(batch_log_dir),
        "output_filename": args.output_filename,
        "max_batch_workers": worker_count,
        "case_count": len(case_inputs),
        "ok_cases": ok_cases,
        "failed_cases": len(case_inputs) - ok_cases,
        "runs": summaries,
    }
    summary_path = batch_root / args.batch_summary_filename
    write_json(summary_path, batch_payload)
    write_json(batch_log_dir / "batch_summary.json", batch_payload)
    emit_step_done_log(
        prefix="NL2ER_DIFF_BATCH",
        step="batch_done",
        elapsed_seconds=time.perf_counter() - batch_started_at,
        ok=ok_cases == len(case_inputs),
        cases=len(case_inputs),
        ok_cases=ok_cases,
        failed_cases=len(case_inputs) - ok_cases,
        summary=str(summary_path),
    )
    if args.print_output:
        print(json.dumps(batch_payload, ensure_ascii=False, indent=2))
    return (0 if ok_cases == len(case_inputs) else 1), batch_payload


def main() -> int:
    parser = build_arg_parser()
    args = parser.parse_args()
    raw_input_path = args.input_path_option or args.input_path
    if not raw_input_path:
        parser.error("input_path is required.")

    try:
        batch_root, case_inputs = discover_case_inputs(raw_input_path)
        if len(case_inputs) == 1:
            exit_code, _ = run_single_from_args(args, case_inputs[0])
            return exit_code

        if args.output_path is not None:
            parser.error("--output-path can only be used for a single nl2er_output.json input.")
        exit_code, _ = run_batch_from_args(
            args,
            batch_root=batch_root,
            case_inputs=case_inputs,
        )
        return exit_code
    except KeyboardInterrupt:
        print("[NL2ER_DIFF] interrupted by user")
        return 130
    except SystemExit:
        raise
    except Exception as exc:
        print(f"[NL2ER_DIFF] failed: {exc}")
        print(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
