from __future__ import annotations

import argparse
import copy
import csv
import json
import re
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.nl2sql.base import NL2SQLRequest, SchemaLinkingRequest
from src.nl2sql.defaults import (
    DEFAULT_ENGINE_SCRIPT,
    DEFAULT_NL2SQL_ENGINE_SCRIPT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SPIDER2_ROOT,
)
from src.nl2sql.db_resource_locator import resolve_database_resource
from src.nl2sql.registry import get_engine_provider, resolve_engine_runtime
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse
from src.utils.run_log import (
    build_timestamp,
    emit_step_done_log,
    resolve_output_path,
    resolve_run_dir,
    resolve_run_log_dir,
)


DEFAULT_INPUT_PATH = Path("er_output_test.json")
DEFAULT_OUTPUT_FILENAME = "er2data_output.json"
DEFAULT_ANALYSIS_FILENAME = "er2data_analysis.json"
DEFAULT_FINAL_QUERY_FILENAME = "final_query_output.json"
DEFAULT_DB_ID = "NEW_YORK_CITIBIKE_1"
DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_LOG_ROOT = Path("log/er2data")
DEFAULT_METADATA_ROOT = Path("metadata")
DEFAULT_ER2QUERY_TEMPLATE_NAME = "ER2Data_er2query_st1_v1.0.md"
LEGACY_ER2QUERY_TEMPLATE_NAME = "ER2Data_er2query_st1_v0.3.md"
PROMPT_TEMPLATE_KEY = "er2data_unit_to_query"
ANALYSIS_TEMPLATE_NAME = "ER2Data_query2unit_check_st2_v0.md"
ANALYSIS_TEMPLATE_KEY = "er2data_query_to_unit_check"
FINAL_QUERY_TEMPLATE_NAME = "ER2Data_final_query_st3_v0.md"
FINAL_QUERY_TEMPLATE_KEY = "er2data_final_query"
SAFE_FILE_RE = re.compile(r"[^\w.-]+", re.UNICODE)
SQL_SYMBOL_SAFE_RE = re.compile(r"[^\w]+", re.UNICODE)
ANALYSIS_RESULT_SAMPLE_ROW_LIMIT = 5
ANALYSIS_RESULT_SAMPLE_COLUMN_LIMIT = 12
ANALYSIS_RESULT_SAMPLE_VALUE_MAX_CHARS = 160
SIDECAR_INPUT_FILENAMES = ("input.json", "nl2er_input.json")
SEMANTIC_UNIT_OUTPUT_CONTRACT = """[Semantic Unit Output Contract]

This SQL implements one semantic unit and its final output columns will be reused later to assemble a larger final query.

Requirements for the outermost SELECT of this semantic-unit SQL:
- Every output column in the outermost SELECT must use an explicit alias with `AS`.
- Do not use `SELECT *` in the outermost SELECT.
- Every output alias must represent the semantic attribute alias of this unit, not the raw physical database column name.
- If a physical source column or expression represents a semantic attribute, project it as `<source_expression> AS <semantic_alias>`.
- The outermost output aliases must be stable and reusable across later SQL composition.
- Output aliases may keep explicit Chinese or other original semantic names when appropriate.
""".strip()
RELATIONSHIP_OUTPUT_ALIAS_CONTRACT = """[Relationship Output Alias Contract]

This SQL implements one relationship or connection semantic unit.

For the outermost SELECT of this relationship or connection SQL:
- Every output column must use an explicit alias with `AS`.
- Do not use `SELECT *`.
- Every relationship-owned or connection-owned attribute must be projected using its semantic attribute alias.
- Every participant identifier used by this relationship must be projected using the required semantic alias defined below.
""".strip()
PARTICIPANT_IDENTIFIER_DEPENDENCY_CONTRACT = """[Participant Identifier Dependency Contract]

This relationship or connection depends on identifier attributes from its participant entities.

Rules:
- If a relationship output column semantically represents a participant entity identifier, reuse the required semantic alias for that participant identifier.
- The physical source table does not need to be the same as in the entity SQL.
- The physical source column does not need to be the same as in the entity SQL.
- A different expression may be used, but it must still be projected with the required semantic alias.
- Do not invent a new alias for a participant identifier when a required semantic alias is already defined.
""".strip()
RELATIONSHIP_ALIAS_EXAMPLES = """Examples:
- If the association uses `buyer_no` for the buyer Customer identifier, project `buyer_no AS 客户_id`.
- If the association uses `sales_order_no` for the Order identifier, project `sales_order_no AS 订单_id`.
- If the association uses `COALESCE(account_id, user_id)` for the Customer identifier, project `COALESCE(account_id, user_id) AS 客户_id`.
- If the same entity participates with multiple roles, use the required role-aware aliases, for example `buyer_no AS 买方__客户_id` and `seller_no AS 卖方__客户_id`.
""".strip()


@dataclass(slots=True)
class UnitTask:
    unit_id: str
    source_index: int
    target_unit_type: str
    target_unit_name: str
    target_unit: dict[str, Any]
    applied_conditions: list[dict[str, Any]]
    condition_selection: list[dict[str, Any]]


def read_json(path: str | Path) -> dict[str, Any]:
    file_path = Path(path)
    with file_path.open("r", encoding="utf-8") as file:
        payload = json.load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {file_path}, got {type(payload).__name__}")
    return payload


def iter_sidecar_payloads(path: str | Path) -> list[tuple[Path, dict[str, Any]]]:
    input_path = Path(path)
    payloads: list[tuple[Path, dict[str, Any]]] = []
    for candidate_name in SIDECAR_INPUT_FILENAMES:
        candidate_path = input_path.with_name(candidate_name)
        if not candidate_path.exists():
            continue
        try:
            payload = read_json(candidate_path)
        except Exception:
            continue
        payloads.append((candidate_path, payload))
    return payloads


def read_sidecar_context(path: str | Path) -> dict[str, str]:
    resolved = {
        "question_id": "",
        "db_id": "",
        "user_intent": "",
        "db_hint": "",
        "external_knowledge": "",
    }
    for _candidate_path, payload in iter_sidecar_payloads(path):
        if not resolved["question_id"]:
            resolved["question_id"] = str(
                payload.get("question_id") or payload.get("instance_id") or ""
            ).strip()
        if not resolved["db_id"]:
            resolved["db_id"] = str(payload.get("db_id") or "").strip()
        if not resolved["user_intent"]:
            resolved["user_intent"] = str(
                payload.get("user_intent") or payload.get("instruction") or ""
            ).strip()
        if not resolved["db_hint"]:
            resolved["db_hint"] = str(payload.get("db_hint") or "").strip()
        if not resolved["external_knowledge"]:
            candidate_knowledge = payload.get("external_knowledge")
            if isinstance(candidate_knowledge, str) and candidate_knowledge.strip():
                resolved["external_knowledge"] = candidate_knowledge.strip()
    return resolved


def read_question_id(path: str | Path) -> str:
    payload = read_json(path)
    question_id = str(payload.get("question_id") or payload.get("instance_id") or "").strip()
    if question_id:
        return question_id
    return read_sidecar_context(path).get("question_id", "").strip()


def read_external_knowledge_from_sidecar(path: str | Path) -> str:
    return read_sidecar_context(path).get("external_knowledge", "").strip()


def resolve_er2query_prompt_context(
    *,
    er_model: dict[str, Any],
    input_path: str | Path,
    external_knowledge: str = "",
) -> dict[str, str]:
    sidecar_context = read_sidecar_context(input_path)

    user_intent = str(er_model.get("user_intent") or er_model.get("instruction") or "").strip()
    if not user_intent:
        user_intent = sidecar_context.get("user_intent", "").strip()

    db_hint = str(er_model.get("db_hint") or "").strip()
    if not db_hint:
        db_hint = sidecar_context.get("db_hint", "").strip()

    resolved_external_knowledge = str(external_knowledge or "").strip()
    if not resolved_external_knowledge:
        candidate = er_model.get("external_knowledge")
        if isinstance(candidate, str):
            resolved_external_knowledge = candidate.strip()
    if not resolved_external_knowledge:
        resolved_external_knowledge = sidecar_context.get("external_knowledge", "").strip()

    return {
        "user_intent": user_intent,
        "db_hint": db_hint,
        "external_knowledge": resolved_external_knowledge,
    }


def cli_option_provided(option_name: str) -> bool:
    flag = f"--{option_name}"
    for argument in sys.argv[1:]:
        if argument == flag or argument.startswith(flag + "="):
            return True
    return False


def normalize_nl2er_attr_list(
    value: Any,
    *,
    location: str,
    field_name: str,
) -> list[dict[str, str]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"`{field_name}` must be a list in {location}")
    normalized: list[dict[str, str]] = []
    seen_names: set[str] = set()
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise ValueError(
                f"`{field_name}[{index}]` must be a string in {location}"
            )
        name = item.strip()
        if not name:
            continue
        if name in seen_names:
            continue
        normalized.append({"name": name})
        seen_names.add(name)
    return normalized


def normalize_nl2er_identifier_attrs(
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
            raise ValueError(f"`{field_name}[{index}]` must be a string in {location}")
        text = item.strip()
        if not text or text in seen:
            continue
        normalized.append(text)
        seen.add(text)
    return normalized


def normalize_nl2er_participants(
    value: Any,
    *,
    location: str,
) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"`participants` must be a list in {location}")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        participant_location = f"{location}.participants[{index}]"
        if not isinstance(item, dict):
            raise ValueError(
                f"`participants[{index}]` must be an object in {location}"
            )
        entity_name = str(item.get("entity") or "").strip()
        role_name = str(item.get("role") or "").strip()
        if not entity_name:
            raise ValueError(f"`participants[{index}].entity` is required in {location}")
        normalized.append(
            {
                "entity": entity_name,
                "role": role_name,
                "cardinality": "unknown",
                "identifier_attrs": normalize_nl2er_identifier_attrs(
                    item.get("anchor_attribute"),
                    location=participant_location,
                    field_name="anchor_attribute",
                ),
            }
        )
    return normalized


def normalize_nl2er_relationship_unit(
    value: Any,
    *,
    location: str,
    name_field: str,
    source_type: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"`{location}` must be an object")

    unit_name = str(value.get(name_field) or "").strip()
    if not unit_name:
        raise ValueError(f"`{location}.{name_field}` is required")

    return {
        "name": unit_name,
        "grain": str(value.get("grain") or "").strip(),
        "desc": str(value.get("desc") or value.get("link_condition") or "").strip(),
        "participants": normalize_nl2er_participants(
            value.get("participants"),
            location=location,
        ),
        "attrs": normalize_nl2er_attr_list(
            value.get("attributes"),
            location=location,
            field_name="attributes",
        ),
        "source_type": source_type,
        "link_condition": str(value.get("link_condition") or "").strip(),
    }


def normalize_nl2er_conditions(value: Any, *, input_path: str | Path) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"`conditions` must be a list in {Path(input_path)}")
    normalized: list[dict[str, Any]] = []
    for index, condition in enumerate(value):
        location = f"{Path(input_path)}.conditions[{index}]"
        if not isinstance(condition, dict):
            raise ValueError(f"`conditions[{index}]` must be an object in {Path(input_path)}")
        normalized_condition: dict[str, Any] = {}
        normalized_condition["name"] = str(condition.get("condition_name") or "").strip()
        if not normalized_condition["name"]:
            raise ValueError(f"`conditions[{index}].condition_name` is required in {Path(input_path)}")
        raw_target = condition.get("targets", [])
        if raw_target is None:
            normalized_condition["target"] = []
        elif isinstance(raw_target, str):
            normalized_condition["target"] = [raw_target.strip()] if raw_target.strip() else []
        elif isinstance(raw_target, list):
            targets: list[str] = []
            for target_index, item in enumerate(raw_target):
                if not isinstance(item, str):
                    raise ValueError(
                        f"`conditions[{index}].targets[{target_index}]` must be a string in {location}"
                    )
                text = item.strip()
                if text:
                    targets.append(text)
            normalized_condition["target"] = targets
        else:
            raise ValueError(f"`conditions[{index}].targets` must be a string or list in {location}")
        normalized_condition["condition_name"] = normalized_condition["name"]
        normalized_condition["targets"] = list(normalized_condition["target"])
        normalized_condition["condition_type"] = str(condition.get("condition_type") or "").strip()
        normalized_condition["condition_desc"] = str(condition.get("description") or "").strip()
        normalized_condition["description"] = normalized_condition["condition_desc"]
        normalized_condition["basis"] = []
        normalized.append(normalized_condition)
    return normalized


def normalize_er_input_payload(
    payload: dict[str, Any],
    *,
    input_path: str | Path,
    merge_connections_into_relationships: bool = False,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object at {Path(input_path)}, got {type(payload).__name__}")
    if "entity_types" in payload or "relationship_types" in payload:
        raise ValueError(
            "Legacy ER input format with `entity_types` / `relationship_types` is no longer supported. "
            "Regenerate the NL2ER output."
        )

    entities = payload.get("entities", [])
    relations = payload.get("relations", [])
    conditions = payload.get("conditions", [])
    connections = payload.get("connections", [])

    if not isinstance(entities, list):
        raise ValueError(f"`entities` must be a list in {Path(input_path)}")
    if not isinstance(relations, list):
        raise ValueError(f"`relations` must be a list in {Path(input_path)}")
    if connections is not None and not isinstance(connections, list):
        raise ValueError(f"`connections` must be a list in {Path(input_path)}")

    normalized_payload = dict(payload)

    normalized_entities: list[dict[str, Any]] = []
    for index, entity in enumerate(entities):
        location = f"{Path(input_path)}.entities[{index}]"
        if not isinstance(entity, dict):
            raise ValueError(f"`entities[{index}]` must be an object in {Path(input_path)}")
        entity_name = str(entity.get("entity_name") or "").strip()
        if not entity_name:
            raise ValueError(f"`entities[{index}].entity_name` is required in {Path(input_path)}")
        normalized_entities.append(
            {
                "name": entity_name,
                "grain": str(entity.get("grain") or "").strip(),
                "desc": str(entity.get("desc") or "").strip(),
                "role": "",
                "attrs": normalize_nl2er_attr_list(
                    entity.get("attributes"),
                    location=location,
                    field_name="attributes",
                ),
                "identifier_attrs": normalize_nl2er_identifier_attrs(
                    entity.get("primary_key"),
                    location=location,
                    field_name="primary_key",
                ),
                "source_type": "entity",
            }
        )

    normalized_relations: list[dict[str, Any]] = []
    for index, relation in enumerate(relations):
        location = f"{Path(input_path)}.relations[{index}]"
        normalized_relations.append(
            normalize_nl2er_relationship_unit(
                relation,
                location=location,
                name_field="relation_name",
                source_type="relation",
            )
        )

    normalized_connections: list[dict[str, Any]] = []
    for index, connection in enumerate(connections or []):
        location = f"{Path(input_path)}.connections[{index}]"
        normalized_connections.append(
            normalize_nl2er_relationship_unit(
                connection,
                location=location,
                name_field="connection_name",
                source_type="connection",
            )
        )

    normalized_payload["entities"] = normalized_entities
    normalized_payload["relations"] = normalized_relations
    normalized_payload["connections"] = normalized_connections
    normalized_payload["conditions"] = normalize_nl2er_conditions(
        conditions,
        input_path=input_path,
    )
    return normalized_payload


def write_json(path: str | Path, payload: dict[str, Any]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def to_pretty_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def unique_strings(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = value.strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def to_safe_sql_symbol(value: str, *, fallback: str) -> str:
    raw_value = str(value or "").strip()
    text = re.sub(r"_+", "_", SQL_SYMBOL_SAFE_RE.sub("_", raw_value)).strip("_")
    if not text:
        text = fallback
    if text and text[0].isdigit():
        text = "_" + text
    return text


def format_alias_list(aliases: list[str]) -> str:
    if not aliases:
        return "(none)"
    return ", ".join(f"`{alias}`" for alias in aliases)


def safe_file_stem(value: str) -> str:
    raw_value = str(value or "").strip()
    cleaned = re.sub(r"_+", "_", SAFE_FILE_RE.sub("_", raw_value)).strip("._-")
    if not cleaned:
        cleaned = "item"
    return cleaned


def shorten_text(text: str | None, limit: int = 1200) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


def sanitize_schema_linking_response(response: dict[str, Any]) -> dict[str, Any]:
    cleaned = copy.deepcopy(response)
    request = cleaned.get("request")
    if isinstance(request, dict) and request.get("api_key"):
        request["api_key"] = "***REDACTED***"
    return cleaned


def sanitize_nl2sql_response(response: dict[str, Any]) -> dict[str, Any]:
    return copy.deepcopy(response)


def parse_table_fqn(fullname: str) -> dict[str, str]:
    parts = [part.strip() for part in fullname.split(".") if part.strip()]
    if len(parts) < 3:
        return {"fullname": fullname}
    return {
        "fullname": fullname,
        "database": ".".join(parts[:-2]),
        "schema": parts[-2],
        "table": parts[-1],
    }


def parse_column_fqn(fullname: str) -> dict[str, str]:
    parts = [part.strip() for part in fullname.split(".") if part.strip()]
    if len(parts) < 4:
        return {"fullname": fullname}
    return {
        "fullname": fullname,
        "database": ".".join(parts[:-3]),
        "schema": parts[-3],
        "table": parts[-2],
        "column": parts[-1],
    }


def extract_basis_roots(condition: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    for basis in condition.get("basis", []):
        if not isinstance(basis, str):
            continue
        head = basis.split(".", 1)[0].strip()
        if head:
            roots.add(head)
    return roots


def extract_condition_targets(condition: dict[str, Any]) -> list[str]:
    raw_target = condition.get("target", [])
    if isinstance(raw_target, str):
        text = raw_target.strip()
        return [text] if text else []
    return unique_strings(raw_target)


def extract_condition_target_roots(condition: dict[str, Any]) -> set[str]:
    roots: set[str] = set()
    for target in extract_condition_targets(condition):
        head = target.split(".", 1)[0].strip()
        if head:
            roots.add(head)
    return roots


def extract_linking_items(payload: dict[str, Any], key: str) -> list[str]:
    alias_map = {
        "gen_tb": ["gen_tb", "linked_tables"],
        "gen_col": ["gen_col", "linked_columns"],
    }
    candidate_keys = alias_map.get(key, [key])
    parsed_info = payload.get("parsed_info")
    for source in (parsed_info, payload):
        if not isinstance(source, dict):
            continue
        for candidate_key in candidate_keys:
            items = unique_strings(source.get(candidate_key, []))
            if items:
                return items
    return []


def extract_schema_linking_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {}

    nested_payload = payload.get("schema_linking")
    if isinstance(nested_payload, dict):
        return nested_payload
    return payload


class ER2DataRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        er2query_template_name: str = DEFAULT_ER2QUERY_TEMPLATE_NAME,
        question_model_config: str | None = None,
        schema_link_model_config: str | None = None,
        nl2sql_model_config: str | None = None,
        include_desc_in_er2query: bool = True,
        include_conditions_in_er2query: bool = False,
        include_conditions_in_sql2nl: bool = False,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.er2query_template_name = str(er2query_template_name).strip()
        self.include_desc_in_er2query = include_desc_in_er2query
        self.include_conditions_in_er2query = include_conditions_in_er2query
        self.include_conditions_in_sql2nl = include_conditions_in_sql2nl
        self.question_prompt_dir = self.log_dir / "question_prompts"
        self.question_response_dir = self.log_dir / "question_responses"
        self.analysis_prompt_dir = self.log_dir / "analysis_prompts"
        self.analysis_response_dir = self.log_dir / "analysis_responses"
        self.schema_link_result_dir = self.log_dir / "schema_linking_results"
        self.schema_link_wrapper_dir = self.log_dir / "schema_linking_wrapper"
        self.nl2sql_result_dir = self.log_dir / "nl2sql_results"
        self.nl2sql_wrapper_dir = self.log_dir / "nl2sql_wrapper"

        for directory in (
            self.log_dir,
            self.question_prompt_dir,
            self.question_response_dir,
            self.analysis_prompt_dir,
            self.analysis_response_dir,
            self.schema_link_result_dir,
            self.schema_link_wrapper_dir,
            self.nl2sql_result_dir,
            self.nl2sql_wrapper_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        settings = load_settings()
        self.question_model_config_name = question_model_config or settings.llm.default_model
        self.schema_link_model_config_name = (
            schema_link_model_config or self.question_model_config_name
        )
        self.nl2sql_model_config_name = (
            nl2sql_model_config or self.schema_link_model_config_name
        )
        self.question_llm_config = settings.llm.get(self.question_model_config_name)
        self.schema_link_llm_config = settings.llm.get(self.schema_link_model_config_name)
        settings.llm.get(self.nl2sql_model_config_name)
        self.question_llm = LLMClient(self.question_llm_config)

        self.prompt_builder = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=True,
        )
        self.prompt_builder.register_template(
            name=PROMPT_TEMPLATE_KEY,
            template_name=self.er2query_template_name,
            required_vars=["target_unit_type", "target_unit", "conditions"],
            default_vars={
                "user_intent": "",
                "db_hint": "",
                "external_knowledge": "",
                "conditions": [],
            },
            description="Rewrite one ER unit into a natural-language retrieval question.",
        )

        self.prompt_builder.register_template(
            name=ANALYSIS_TEMPLATE_KEY,
            template_name=ANALYSIS_TEMPLATE_NAME,
            required_vars=[
                "unit_nl_query",
                "external_knowledge",
                "target_unit_definition",
                "schema_linking_json",
                "sql_result_evidence_json",
                "result_sql",
            ],
            default_vars={},
            description="Analyze whether one SQL precisely and completely implements a target ER unit.",
        )
        self.prompt_builder.register_template(
            name=FINAL_QUERY_TEMPLATE_KEY,
            template_name=FINAL_QUERY_TEMPLATE_NAME,
            required_vars=[
                "db_id",
                "user_question",
                "external_knowledge",
                "response_mode",
                "mode_requirement",
            ],
            default_vars={},
            description="Generate the final SQL query from prepared ER-derived CTEs.",
        )

    @staticmethod
    def build_prompt_participants(target_unit: dict[str, Any]) -> list[dict[str, Any]]:
        prompt_participants: list[dict[str, Any]] = []
        for participant in ensure_dict_list(target_unit.get("participants")):
            participant_payload: dict[str, Any] = {}
            entity_name = str(participant.get("entity") or "").strip()
            if entity_name:
                participant_payload["entity"] = entity_name

            role_name = str(participant.get("role") or "").strip()
            if role_name:
                participant_payload["role"] = role_name

            identifier_attrs = unique_strings(participant.get("identifier_attrs", []))
            if identifier_attrs:
                participant_payload["identifier_attrs"] = identifier_attrs

            if participant_payload:
                prompt_participants.append(participant_payload)
        return prompt_participants

    def build_prompt_target_unit(
        self,
        target_unit_type: str,
        target_unit: dict[str, Any],
    ) -> dict[str, Any]:
        prompt_target_unit: dict[str, Any] = {}

        name = str(target_unit.get("name") or "").strip()
        if name:
            prompt_target_unit["name"] = name

        desc = str(target_unit.get("desc") or "").strip()
        if self.include_desc_in_er2query and desc:
            prompt_target_unit["desc"] = desc

        grain = str(target_unit.get("grain") or "").strip()
        if grain:
            prompt_target_unit["grain"] = grain

        if target_unit_type == "entity":
            identifier_attrs = unique_strings(target_unit.get("identifier_attrs", []))
            if identifier_attrs:
                prompt_target_unit["identifier_attrs"] = identifier_attrs
            attrs = ensure_dict_list(target_unit.get("attrs"))
            if attrs:
                prompt_target_unit["attrs"] = copy.deepcopy(attrs)
            return prompt_target_unit

        participants = ER2DataRunner.build_prompt_participants(target_unit)
        if participants:
            prompt_target_unit["participants"] = participants

        if target_unit_type == "relationship":
            attrs = ensure_dict_list(target_unit.get("attrs"))
            if attrs:
                prompt_target_unit["attrs"] = copy.deepcopy(attrs)

        link_condition = str(target_unit.get("link_condition") or "").strip()
        if link_condition:
            prompt_target_unit["link_condition"] = link_condition

        return prompt_target_unit

    @staticmethod
    def build_prompt_conditions(conditions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prompt_conditions: list[dict[str, Any]] = []
        for condition in conditions:
            if not isinstance(condition, dict):
                continue
            payload: dict[str, Any] = {}

            condition_name = str(
                condition.get("condition_name") or condition.get("name") or ""
            ).strip()
            if condition_name:
                payload["condition_name"] = condition_name

            condition_type = str(condition.get("condition_type") or "").strip()
            if condition_type:
                payload["condition_type"] = condition_type

            targets = extract_condition_targets(condition)
            if targets:
                payload["targets"] = targets

            description = str(
                condition.get("description") or condition.get("condition_desc") or ""
            ).strip()
            if description:
                payload["description"] = description

            if payload:
                prompt_conditions.append(payload)
        return prompt_conditions

    def build_units(self, er_model: dict[str, Any]) -> list[UnitTask]:
        entity_types = ensure_dict_list(er_model.get("entities"))
        relation_types = ensure_dict_list(er_model.get("relations"))
        connection_types = ensure_dict_list(er_model.get("connections"))
        conditions = ensure_dict_list(er_model.get("conditions"))
        entity_index = {
            entity["name"]: entity
            for entity in entity_types
            if isinstance(entity.get("name"), str) and entity["name"].strip()
        }

        units: list[UnitTask] = []

        for idx, entity in enumerate(entity_types):
            entity_name = str(entity.get("name", "")).strip()
            if not entity_name:
                continue
            applied_conditions, condition_selection = self.select_conditions_for_unit(
                target_unit_type="entity",
                target_unit=entity,
                all_conditions=conditions,
            )
            units.append(
                UnitTask(
                    unit_id=f"entity::{entity_name}",
                    source_index=idx,
                    target_unit_type="entity",
                    target_unit_name=entity_name,
                    target_unit=copy.deepcopy(entity),
                    applied_conditions=applied_conditions,
                    condition_selection=condition_selection,
                )
            )

        for idx, relationship in enumerate(relation_types):
            relationship_name = str(relationship.get("name", "")).strip()
            if not relationship_name:
                continue
            enriched_relationship = self.enrich_relationship(relationship, entity_index)
            applied_conditions, condition_selection = self.select_conditions_for_unit(
                target_unit_type="relationship",
                target_unit=enriched_relationship,
                all_conditions=conditions,
            )
            units.append(
                UnitTask(
                    unit_id=f"relationship::{relationship_name}",
                    source_index=idx,
                    target_unit_type="relationship",
                    target_unit_name=relationship_name,
                    target_unit=enriched_relationship,
                    applied_conditions=applied_conditions,
                    condition_selection=condition_selection,
                )
            )

        for idx, connection in enumerate(connection_types):
            connection_name = str(connection.get("name", "")).strip()
            if not connection_name:
                continue
            enriched_connection = self.enrich_relationship(connection, entity_index)
            applied_conditions, condition_selection = self.select_conditions_for_unit(
                target_unit_type="connection",
                target_unit=enriched_connection,
                all_conditions=conditions,
            )
            units.append(
                UnitTask(
                    unit_id=f"connection::{connection_name}",
                    source_index=idx,
                    target_unit_type="connection",
                    target_unit_name=connection_name,
                    target_unit=enriched_connection,
                    applied_conditions=applied_conditions,
                    condition_selection=condition_selection,
                )
            )

        return units

    @staticmethod
    def enrich_relationship(
        relationship: dict[str, Any],
        entity_index: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        enriched = copy.deepcopy(relationship)
        participants = ensure_dict_list(enriched.get("participants"))
        enriched_participants: list[dict[str, Any]] = []

        for participant in participants:
            participant_copy = dict(participant)
            entity_name = str(participant_copy.get("entity", "")).strip()
            identifier_attrs = unique_strings(participant_copy.get("identifier_attrs", []))
            if not identifier_attrs:
                entity_def = entity_index.get(entity_name, {})
                identifier_attrs = unique_strings(entity_def.get("identifier_attrs", []))
            participant_copy["identifier_attrs"] = identifier_attrs
            enriched_participants.append(participant_copy)

        enriched["participants"] = enriched_participants
        enriched["attrs"] = ensure_dict_list(enriched.get("attrs"))
        return enriched

    def select_conditions_for_unit(
        self,
        *,
        target_unit_type: str,
        target_unit: dict[str, Any],
        all_conditions: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        unit_name = str(target_unit.get("name", "")).strip()
        participant_entities = {
            str(participant.get("entity", "")).strip()
            for participant in ensure_dict_list(target_unit.get("participants"))
            if str(participant.get("entity", "")).strip()
        }

        applied_conditions: list[dict[str, Any]] = []
        selection_log: list[dict[str, Any]] = []

        for condition in all_conditions:
            condition_name = str(condition.get("name", "")).strip()
            condition_type = str(condition.get("condition_type") or "").strip().lower()
            targets = extract_condition_targets(condition)
            target_roots = extract_condition_target_roots(condition)
            basis_roots = extract_basis_roots(condition)
            reasons: list[str] = []

            if unit_name and unit_name in targets:
                reasons.append("condition.target matches current unit")
            if unit_name and unit_name in basis_roots:
                reasons.append("condition.basis directly references current unit")
            if unit_name and unit_name in target_roots:
                reasons.append("condition.target root matches current unit")

            if target_unit_type in {"relationship", "connection"} and participant_entities:
                if participant_entities.intersection(targets):
                    reasons.append("condition.target matches an association participant")
                if participant_entities.intersection(target_roots):
                    reasons.append("condition.target root matches an association participant")
                if basis_roots.intersection(participant_entities):
                    reasons.append("condition.basis references an association participant")

            selected = bool(reasons)
            selection_log.append(
                {
                    "condition_name": condition_name,
                    "condition_type": condition_type,
                    "selected": selected,
                    "reasons": reasons,
                }
            )
            if selected:
                applied_conditions.append(copy.deepcopy(condition))

        return applied_conditions, selection_log

    def build_prompt(self, unit: UnitTask, *, prompt_context: dict[str, str] | None = None) -> str:
        resolved_prompt_context = prompt_context or {}
        return self.prompt_builder.build_text(
            PROMPT_TEMPLATE_KEY,
            vars={
                "user_intent": str(resolved_prompt_context.get("user_intent") or "").strip(),
                "db_hint": str(resolved_prompt_context.get("db_hint") or "").strip(),
                "external_knowledge": str(
                    resolved_prompt_context.get("external_knowledge") or ""
                ).strip(),
                "target_unit_type": unit.target_unit_type,
                "target_unit": self.build_prompt_target_unit(
                    unit.target_unit_type,
                    unit.target_unit,
                ),
                "conditions": self.build_prompt_conditions(
                    unit.applied_conditions if self.include_conditions_in_er2query else []
                ),
            },
        )

    def generate_questions(
        self,
        units: list[UnitTask],
        *,
        prompt_context: dict[str, str] | None = None,
        max_concurrency: int | None = None,
    ) -> list[dict[str, Any]]:
        prompts: list[str] = []
        results: list[dict[str, Any]] = []

        for unit in units:
            prompt = self.build_prompt(unit, prompt_context=prompt_context)
            prompt_path = self.question_prompt_dir / f"{safe_file_stem(unit.unit_id)}.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            prompts.append(prompt)
            results.append(
                {
                    "unit_id": unit.unit_id,
                    "source_index": unit.source_index,
                    "target_unit_type": unit.target_unit_type,
                    "target_unit_name": unit.target_unit_name,
                    "target_unit": unit.target_unit,
                    "applied_conditions": unit.applied_conditions,
                    "condition_selection": unit.condition_selection,
                    "question": None,
                    "question_generation": {
                        "ok": False,
                        "prompt_path": str(prompt_path),
                        "response_path": str(
                            self.question_response_dir / f"{safe_file_stem(unit.unit_id)}.md"
                        ),
                        "parsed_response": None,
                        "error": None,
                    },
                    "schema_linking": {
                        "ok": None,
                        "skipped": True,
                        "engine_result_path": None,
                        "wrapper_log_path": None,
                        "tables": [],
                        "columns": [],
                        "error": None,
                        "stdout_excerpt": "",
                        "stderr_excerpt": "",
                    },
                    "nl2sql": {
                        "ok": None,
                        "skipped": True,
                        "engine_result_path": None,
                        "wrapper_log_path": None,
                        "result_sql": "",
                        "error": None,
                        "stdout_excerpt": "",
                        "stderr_excerpt": "",
                    },
                }
            )

        raw_responses = self.question_llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(prompts) > 1,
            progress_desc="ER question generation",
        )

        for result, raw_response in zip(results, raw_responses):
            response_path = Path(result["question_generation"]["response_path"])
            response_path.write_text(raw_response or "", encoding="utf-8")

            if not raw_response or not raw_response.strip():
                result["question_generation"]["error"] = "LLM returned empty response."
                continue

            try:
                parsed = json_parse(raw_response)
            except Exception as exc:
                result["question_generation"]["error"] = str(exc)
                continue

            question = parsed.get("question")
            if not isinstance(question, str) or not question.strip():
                result["question_generation"]["error"] = "Parsed JSON does not contain a valid question."
                result["question_generation"]["parsed_response"] = parsed
                continue

            result["question"] = question.strip()
            result["question_generation"]["ok"] = True
            result["question_generation"]["parsed_response"] = parsed

        return results

    def run_schema_linking_batch(
        self,
        unit_results: list[dict[str, Any]],
        *,
        db_id: str,
        max_workers: int,
        provider_name: str,
        runtime,
        timeout_seconds: int | None,
        temperature: float,
        shortlist_trigger: int,
        max_shortlist_tables: int,
        sample_row_limit: int,
        sample_value_max_chars: int,
        similar_tables_hint_limit: int,
    ) -> None:
        runnable_indices = [
            idx for idx, item in enumerate(unit_results)
            if isinstance(item.get("question"), str) and item["question"].strip()
        ]

        if not runnable_indices:
            return

        worker_count = max(1, min(max_workers, len(runnable_indices)))

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    self._run_single_schema_linking,
                    unit_results[idx],
                    db_id=db_id,
                    provider_name=provider_name,
                    runtime=runtime,
                    timeout_seconds=timeout_seconds,
                    temperature=temperature,
                    shortlist_trigger=shortlist_trigger,
                    max_shortlist_tables=max_shortlist_tables,
                    sample_row_limit=sample_row_limit,
                    sample_value_max_chars=sample_value_max_chars,
                    similar_tables_hint_limit=similar_tables_hint_limit,
                ): idx
                for idx in runnable_indices
            }

            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    summary = future.result()
                except Exception as exc:
                    summary = {
                        "ok": False,
                        "skipped": False,
                        "engine_result_path": None,
                        "wrapper_log_path": None,
                        "tables": [],
                        "columns": [],
                        "error": f"Schema linking execution failed: {exc}",
                        "stdout_excerpt": "",
                        "stderr_excerpt": "",
                    }
                unit_results[idx]["schema_linking"] = summary

    def _run_single_schema_linking(
        self,
        unit_result: dict[str, Any],
        *,
        db_id: str,
        provider_name: str,
        runtime,
        timeout_seconds: int | None,
        temperature: float,
        shortlist_trigger: int,
        max_shortlist_tables: int,
        sample_row_limit: int,
        sample_value_max_chars: int,
        similar_tables_hint_limit: int,
    ) -> dict[str, Any]:
        unit_id = str(unit_result["unit_id"])
        unit_file_stem = safe_file_stem(unit_id)
        engine_output_path = (self.schema_link_result_dir / f"{unit_file_stem}.json").resolve()
        wrapper_log_path = (self.schema_link_wrapper_dir / f"{unit_file_stem}.json").resolve()

        provider = get_engine_provider(provider_name)
        result = provider.run_schema_linking(
            SchemaLinkingRequest(
                api_key=self.schema_link_llm_config.api_key,
                base_url=self.schema_link_llm_config.base_url,
                db_id=db_id,
                question=str(unit_result["question"]),
                model=self.schema_link_llm_config.model,
                output_path=str(engine_output_path),
                temperature=temperature,
                shortlist_trigger=shortlist_trigger,
                max_shortlist_tables=max_shortlist_tables,
                sample_row_limit=sample_row_limit,
                sample_value_max_chars=sample_value_max_chars,
                similar_tables_hint_limit=similar_tables_hint_limit,
                timeout_seconds=timeout_seconds,
            ),
            runtime=runtime,
            raise_on_error=False,
        )
        sanitized_response = sanitize_schema_linking_response(result.to_payload())
        write_json(wrapper_log_path, sanitized_response)

        return {
            "ok": result.ok,
            "skipped": False,
            "engine_result_path": str(result.result_path or engine_output_path),
            "wrapper_log_path": str(wrapper_log_path),
            "tables": result.tables,
            "columns": result.columns,
            "error": result.error,
            "stdout_excerpt": shorten_text(result.stdout),
            "stderr_excerpt": shorten_text(result.stderr),
        }

    def run_nl2sql_batch(
        self,
        unit_results: list[dict[str, Any]],
        *,
        db_id: str,
        external_knowledge: str = "",
        target_indices: list[int] | None = None,
        external_knowledge_resolver: Callable[[dict[str, Any]], str] | None = None,
        max_workers: int,
        provider_name: str,
        runtime,
        timeout_seconds: float,
    ) -> None:
        if external_knowledge_resolver is None:
            external_knowledge_resolver = lambda _unit_result: external_knowledge

        candidate_indices = (
            target_indices if target_indices is not None else list(range(len(unit_results)))
        )
        runnable_indices = [
            idx
            for idx in candidate_indices
            if 0 <= idx < len(unit_results)
            for item in [unit_results[idx]]
            if isinstance(item.get("question"), str) and item["question"].strip()
        ]

        if not runnable_indices:
            return

        worker_count = max(1, min(max_workers, len(runnable_indices)))

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_map = {
                executor.submit(
                    self._run_single_nl2sql,
                    unit_results[idx],
                    db_id=db_id,
                    external_knowledge=external_knowledge_resolver(unit_results[idx]),
                    provider_name=provider_name,
                    runtime=runtime,
                    timeout_seconds=timeout_seconds,
                ): idx
                for idx in runnable_indices
            }

            for future in as_completed(future_map):
                idx = future_map[future]
                try:
                    summary_bundle = future.result()
                except Exception as exc:
                    summary_bundle = {
                        "nl2sql": {
                            "ok": False,
                            "skipped": False,
                            "engine_result_path": None,
                            "wrapper_log_path": None,
                            "result_sql": "",
                            "error": f"NL2SQL execution failed: {exc}",
                            "stdout_excerpt": "",
                            "stderr_excerpt": "",
                        },
                        "schema_linking": {
                            "ok": False,
                            "skipped": False,
                            "engine_result_path": None,
                            "wrapper_log_path": None,
                            "tables": [],
                            "columns": [],
                            "error": f"NL2SQL execution failed before embedded schema linking could be read: {exc}",
                            "stdout_excerpt": "",
                            "stderr_excerpt": "",
                        },
                    }
                unit_results[idx]["nl2sql"] = summary_bundle["nl2sql"]
                unit_results[idx]["schema_linking"] = summary_bundle["schema_linking"]

    @staticmethod
    def get_relationship_participant_entities(unit_result: dict[str, Any]) -> list[str]:
        target_unit = unit_result.get("target_unit", {})
        if not isinstance(target_unit, dict):
            return []
        participants = ensure_dict_list(target_unit.get("participants"))
        entity_names: list[str] = []
        seen: set[str] = set()
        for participant in participants:
            entity_name = str(participant.get("entity", "")).strip()
            if not entity_name or entity_name in seen:
                continue
            entity_names.append(entity_name)
            seen.add(entity_name)
        return entity_names

    @staticmethod
    def collect_successful_entity_sql(
        unit_results: list[dict[str, Any]],
    ) -> dict[str, str]:
        entity_sql_by_name: dict[str, str] = {}
        for unit_result in unit_results:
            if str(unit_result.get("target_unit_type") or "").strip() != "entity":
                continue
            entity_name = str(unit_result.get("target_unit_name") or "").strip()
            nl2sql = unit_result.get("nl2sql", {})
            result_sql = str(nl2sql.get("result_sql") or "").strip() if isinstance(nl2sql, dict) else ""
            if not entity_name or not result_sql:
                continue
            entity_sql_by_name[entity_name] = result_sql
        return entity_sql_by_name

    @staticmethod
    def build_entity_output_aliases(target_unit: dict[str, Any]) -> list[str]:
        required_aliases: list[str] = []
        seen_aliases: set[str] = set()

        for identifier_attr in unique_strings(target_unit.get("identifier_attrs", [])):
            alias = to_safe_sql_symbol(identifier_attr, fallback="id")
            if alias in seen_aliases:
                continue
            required_aliases.append(alias)
            seen_aliases.add(alias)

        for attr in ensure_dict_list(target_unit.get("attrs")):
            attr_name = str(attr.get("name") or "").strip()
            if not attr_name:
                continue
            alias = to_safe_sql_symbol(attr_name, fallback="attr")
            if alias in seen_aliases:
                continue
            required_aliases.append(alias)
            seen_aliases.add(alias)

        return required_aliases

    @staticmethod
    def build_entity_alias_contract(target_unit: dict[str, Any]) -> str:
        required_aliases = ER2DataRunner.build_entity_output_aliases(target_unit)
        return "\n".join(
            [
                "[Entity Output Alias Contract]",
                "",
                "This SQL implements one entity semantic unit.",
                "",
                "For this entity unit:",
                "- Every identifier attribute must appear in the outermost SELECT with an explicit semantic alias.",
                "- Every non-identifier attribute required by the entity definition must also appear in the outermost SELECT with an explicit semantic alias.",
                "- If the source column name is different from the semantic attribute alias, still use the semantic attribute alias in the final output.",
                "",
                "Required entity output aliases:",
                f"- {format_alias_list(required_aliases)}",
                "",
                "Examples:",
                "- `customer_no AS 客户_id`",
                "- `customer_name_raw AS 客户名称`",
                "- `COALESCE(account_id, user_id) AS 客户_id`",
            ]
        ).strip()

    @staticmethod
    def build_relationship_required_alias_lines(target_unit: dict[str, Any]) -> list[str]:
        participants = ensure_dict_list(target_unit.get("participants"))
        participant_items: list[dict[str, Any]] = []
        alias_occurrences: dict[tuple[str, str], int] = defaultdict(int)

        for index, participant in enumerate(participants):
            entity_name = str(participant.get("entity") or "").strip()
            if not entity_name:
                continue
            role_name = str(participant.get("role") or "").strip()
            base_aliases: list[str] = []
            seen_base_aliases: set[str] = set()
            for identifier_attr in unique_strings(participant.get("identifier_attrs", [])):
                alias = to_safe_sql_symbol(identifier_attr, fallback="id")
                if alias in seen_base_aliases:
                    continue
                base_aliases.append(alias)
                seen_base_aliases.add(alias)
                alias_occurrences[(entity_name, alias)] += 1

            if not base_aliases:
                base_aliases = ["id"]
                alias_occurrences[(entity_name, "id")] += 1

            participant_items.append(
                {
                    "entity_name": entity_name,
                    "role_name": role_name,
                    "index": index,
                    "base_aliases": base_aliases,
                }
            )

        required_lines: list[str] = []
        for item in participant_items:
            entity_name = str(item["entity_name"])
            role_name = str(item["role_name"])
            index = int(item["index"])
            base_aliases = list(item["base_aliases"])
            has_conflict = any(
                alias_occurrences.get((entity_name, alias), 0) > 1 for alias in base_aliases
            )

            if has_conflict:
                role_fallback = f"participant_{index + 1}"
                safe_role_name = to_safe_sql_symbol(role_name, fallback=role_fallback)
                required_aliases = [f"{safe_role_name}__{alias}" for alias in base_aliases]
                label = f"- Participant role `{role_name or safe_role_name}` of entity `{entity_name}`: "
            else:
                required_aliases = base_aliases
                label = f"- Participant entity `{entity_name}`: "

            required_lines.append(label + format_alias_list(required_aliases))

        if not required_lines:
            required_lines.append("- (none)")
        return required_lines

    def build_entity_external_knowledge(
        self,
        unit_result: dict[str, Any],
        *,
        base_external_knowledge: str,
    ) -> str:
        sections: list[str] = []
        base_text = base_external_knowledge.strip()
        if base_text:
            sections.append(base_text)

        sections.append(SEMANTIC_UNIT_OUTPUT_CONTRACT)
        target_unit = unit_result.get("target_unit", {})
        if isinstance(target_unit, dict):
            sections.append(self.build_entity_alias_contract(target_unit))

        return "\n\n".join(section.strip() for section in sections if section.strip())

    def build_relationship_external_knowledge(
        self,
        unit_result: dict[str, Any],
        *,
        base_external_knowledge: str,
        entity_sql_by_name: dict[str, str],
    ) -> str:
        sections: list[str] = []
        base_text = base_external_knowledge.strip()
        if base_text:
            sections.append(base_text)

        sections.append(SEMANTIC_UNIT_OUTPUT_CONTRACT)
        sections.append(RELATIONSHIP_OUTPUT_ALIAS_CONTRACT)
        sections.append(PARTICIPANT_IDENTIFIER_DEPENDENCY_CONTRACT)
        target_unit = unit_result.get("target_unit", {})
        if isinstance(target_unit, dict):
            sections.append(
                "\n".join(
                    ["[Required Participant Identifier Aliases]", ""]
                    + self.build_relationship_required_alias_lines(target_unit)
                ).strip()
            )
        sections.append(RELATIONSHIP_ALIAS_EXAMPLES)

        dependency_blocks: list[str] = []
        for entity_name in self.get_relationship_participant_entities(unit_result):
            entity_sql = entity_sql_by_name.get(entity_name, "").strip()
            if not entity_sql:
                continue
            dependency_blocks.append(
                "\n".join(
                    [
                        f"[ER entity SQL: {entity_name}]",
                        "```sql",
                        entity_sql,
                        "```",
                    ]
                )
            )

        if dependency_blocks:
            sections.append(
                "Dependent entity SQL implementations:\n\n" + "\n\n".join(dependency_blocks)
            )

        return "\n\n".join(section.strip() for section in sections if section.strip())

    def _run_single_nl2sql(
        self,
        unit_result: dict[str, Any],
        *,
        db_id: str,
        external_knowledge: str,
        provider_name: str,
        runtime,
        timeout_seconds: float,
    ) -> dict[str, dict[str, Any]]:
        unit_id = str(unit_result["unit_id"])
        unit_file_stem = safe_file_stem(unit_id)
        engine_output_path = (self.nl2sql_result_dir / f"{unit_file_stem}.json").resolve()
        wrapper_log_path = (self.nl2sql_wrapper_dir / f"{unit_file_stem}.json").resolve()

        provider = get_engine_provider(provider_name)
        result = provider.run_nl2sql(
            NL2SQLRequest(
                question=str(unit_result["question"]),
                db_id=db_id,
                external_knowledge=external_knowledge,
                llm_config_name=self.nl2sql_model_config_name,
                output_path=str(engine_output_path),
                timeout_seconds=timeout_seconds,
            ),
            runtime=runtime,
            raise_on_error=False,
        )

        sanitized_response = sanitize_nl2sql_response(result.to_payload())
        write_json(wrapper_log_path, sanitized_response)
        stdout_excerpt = shorten_text(result.stdout)
        stderr_excerpt = shorten_text(result.stderr)

        nl2sql_summary = {
            "ok": bool(result.sql),
            "skipped": False,
            "engine_result_path": str(result.result_path or engine_output_path),
            "wrapper_log_path": str(wrapper_log_path),
            "result_sql": result.sql,
            "error": None if result.sql else result.error,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        }
        schema_linking_ok = bool(result.tables or result.columns)
        schema_linking_summary = {
            "ok": schema_linking_ok,
            "skipped": False,
            "engine_result_path": str(
                result.schema_linking_result_path or result.result_path or engine_output_path
            ),
            "wrapper_log_path": str(wrapper_log_path),
            "tables": result.tables,
            "columns": result.columns,
            "error": None if schema_linking_ok else (result.error or "NL2SQL result did not include schema linking data."),
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        }

        return {
            "nl2sql": nl2sql_summary,
            "schema_linking": schema_linking_summary,
        }

    @staticmethod
    def build_schema_linking_summary_from_nl2sql_result(
        *,
        engine_result: dict[str, Any],
        fallback_engine_result_path: str | None,
        wrapper_log_path: str | None,
        fallback_error: str | None,
        stdout_excerpt: str,
        stderr_excerpt: str,
    ) -> dict[str, Any]:
        artifacts = engine_result.get("artifacts")
        schema_linking_artifact_path = None
        if isinstance(artifacts, dict):
            artifact_path = artifacts.get("schema_linking_path")
            if isinstance(artifact_path, str) and artifact_path.strip():
                schema_linking_artifact_path = artifact_path.strip()

        schema_linking_payload = extract_schema_linking_payload(engine_result)
        artifact_read_error = None
        if not schema_linking_payload and schema_linking_artifact_path:
            try:
                schema_linking_payload = extract_schema_linking_payload(
                    read_json(schema_linking_artifact_path)
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                artifact_read_error = (
                    f"Failed to read schema linking artifact at "
                    f"{schema_linking_artifact_path}: {exc}"
                )

        linked_tables = unique_strings(schema_linking_payload.get("linked_tables", []))
        linked_columns = unique_strings(schema_linking_payload.get("linked_columns", []))
        selected_groups = ensure_dict_list(schema_linking_payload.get("selected_groups"))
        ok = bool(schema_linking_payload) and bool(
            linked_tables or linked_columns or selected_groups
        )
        error = artifact_read_error or fallback_error
        if not ok and error is None:
            error = "NL2SQL result did not include schema linking data."

        return {
            "ok": ok,
            "skipped": False,
            "engine_result_path": schema_linking_artifact_path or fallback_engine_result_path,
            "wrapper_log_path": wrapper_log_path,
            "tables": [parse_table_fqn(fullname) for fullname in linked_tables],
            "columns": [parse_column_fqn(fullname) for fullname in linked_columns],
            "error": error,
            "stdout_excerpt": stdout_excerpt,
            "stderr_excerpt": stderr_excerpt,
        }

    @staticmethod
    def aggregate_schema_hits(unit_results: list[dict[str, Any]]) -> dict[str, Any]:
        table_to_units: dict[str, set[str]] = defaultdict(set)
        column_to_units: dict[str, set[str]] = defaultdict(set)
        table_meta: dict[str, dict[str, str]] = {}
        column_meta: dict[str, dict[str, str]] = {}

        for unit_result in unit_results:
            unit_id = str(unit_result.get("unit_id", ""))
            schema_linking = unit_result.get("schema_linking", {})
            if not isinstance(schema_linking, dict) or not schema_linking.get("ok"):
                continue

            for table in schema_linking.get("tables", []):
                if not isinstance(table, dict):
                    continue
                fullname = str(table.get("fullname", "")).strip()
                if not fullname:
                    continue
                table_to_units[fullname].add(unit_id)
                table_meta[fullname] = table

            for column in schema_linking.get("columns", []):
                if not isinstance(column, dict):
                    continue
                fullname = str(column.get("fullname", "")).strip()
                if not fullname:
                    continue
                column_to_units[fullname].add(unit_id)
                column_meta[fullname] = column

        aggregated_tables = [
            {
                **table_meta[fullname],
                "hit_count": len(unit_ids),
                "unit_ids": sorted(unit_ids),
            }
            for fullname, unit_ids in table_to_units.items()
        ]
        aggregated_tables.sort(key=lambda item: (-item["hit_count"], item["fullname"]))

        aggregated_columns = [
            {
                **column_meta[fullname],
                "hit_count": len(unit_ids),
                "unit_ids": sorted(unit_ids),
            }
            for fullname, unit_ids in column_to_units.items()
        ]
        aggregated_columns.sort(key=lambda item: (-item["hit_count"], item["fullname"]))

        return {
            "tables": aggregated_tables,
            "columns": aggregated_columns,
        }

    @staticmethod
    def build_minimal_output(unit_results: list[dict[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}

        for unit_result in unit_results:
            unit_name = str(unit_result.get("target_unit_name", "")).strip()
            if not unit_name:
                continue
            if unit_name in output:
                raise ValueError(f"Duplicate target_unit_name detected in output: {unit_name}")

            schema_linking = unit_result.get("schema_linking", {})
            tables: list[str] = []
            columns: list[str] = []

            if isinstance(schema_linking, dict):
                for table in schema_linking.get("tables", []):
                    if not isinstance(table, dict):
                        continue
                    fullname = str(table.get("fullname", "")).strip()
                    if fullname:
                        tables.append(fullname)

                for column in schema_linking.get("columns", []):
                    if not isinstance(column, dict):
                        continue
                    fullname = str(column.get("fullname", "")).strip()
                    if fullname:
                        columns.append(fullname)

            output[unit_name] = {
                "question": str(unit_result.get("question") or ""),
                "schema_linking": {
                    "tables": tables,
                    "columns": columns,
                },
                "result_sql": str(
                    unit_result.get("nl2sql", {}).get("result_sql", "")
                    if isinstance(unit_result.get("nl2sql"), dict)
                    else ""
                ),
                "nl2sql_result": bool(
                    unit_result.get("nl2sql", {}).get("ok")
                    if isinstance(unit_result.get("nl2sql"), dict)
                    else False
                ),
            }

        return output

    def build_final_query_from_available_ctes(
        self,
        *,
        unit_results: list[dict[str, Any]],
        user_question: str = "",
        input_path: str | Path | None = None,
        db_id: str = "",
        base_external_knowledge: str = "",
        max_retry: int = 3,
    ) -> dict[str, Any]:
        resolved_question = str(user_question or "").strip()
        resolved_db_id = str(db_id or "").strip()
        resolved_external_knowledge = str(base_external_knowledge or "").strip()
        resolved_db_hint = ""

        candidate_sidecars: list[Path] = []
        if input_path is not None:
            input_file = Path(input_path)
            for candidate_name in ("input.json", "nl2er_input.json"):
                candidate_sidecars.append(input_file.with_name(candidate_name))

        for candidate_path in candidate_sidecars:
            if not candidate_path.exists():
                continue
            try:
                payload = read_json(candidate_path)
            except Exception:
                continue

            if not resolved_question:
                resolved_question = str(
                    payload.get("user_intent") or payload.get("instruction") or ""
                ).strip()
            if not resolved_db_id:
                resolved_db_id = str(payload.get("db_id") or "").strip()
            if not resolved_db_hint:
                resolved_db_hint = str(payload.get("db_hint") or "").strip()
            if not resolved_external_knowledge:
                candidate_knowledge = payload.get("external_knowledge")
                if isinstance(candidate_knowledge, str) and candidate_knowledge.strip():
                    resolved_external_knowledge = candidate_knowledge.strip()

        if not resolved_question:
            resolved_question = "Answer the original user request using the available ER-derived SQL units."

        def is_safe_sql_identifier(value: str) -> bool:
            return bool(re.fullmatch(r"(?!\d)\w+", value, flags=re.UNICODE))

        def quote_identifier(value: str) -> str:
            return '"' + value.replace('"', '""') + '"'

        def render_identifier(value: str) -> str:
            return value if is_safe_sql_identifier(value) else quote_identifier(value)

        def indent_block(text: str, prefix: str = "    ") -> str:
            lines = text.splitlines() or [""]
            return "\n".join(f"{prefix}{line}" if line else prefix.rstrip() for line in lines)

        def strip_sql_fences(text: str) -> str:
            stripped = str(text or "").strip()
            if stripped.startswith("```"):
                stripped = re.sub(r"^```[A-Za-z0-9_-]*\s*", "", stripped)
                stripped = re.sub(r"\s*```$", "", stripped)
            return stripped.strip()

        def extract_sql_text(text: str) -> str:
            cleaned = strip_sql_fences(text)
            if not cleaned:
                return ""
            match = re.search(r"\b(WITH|SELECT)\b", cleaned, flags=re.IGNORECASE)
            if match:
                return cleaned[match.start() :].strip()
            return cleaned

        def read_result_columns(unit_result: dict[str, Any]) -> list[str]:
            nl2sql = unit_result.get("nl2sql", {})
            engine_result_path = (
                str(nl2sql.get("engine_result_path") or "").strip()
                if isinstance(nl2sql, dict)
                else ""
            )

            csv_path_value = ""
            if engine_result_path:
                try:
                    engine_result = read_json(engine_result_path)
                    final_payload = engine_result.get("final", {})
                    if isinstance(final_payload, dict):
                        csv_path_value = str(final_payload.get("csv_path") or "").strip()
                    if not csv_path_value:
                        for stage in ensure_dict_list(engine_result.get("stages")):
                            if bool(stage.get("ok")) and stage.get("final_csv_path"):
                                csv_path_value = str(stage.get("final_csv_path") or "").strip()
                                break
                except Exception:
                    csv_path_value = ""

            if csv_path_value:
                csv_file = Path(csv_path_value)
                if csv_file.exists():
                    try:
                        with csv_file.open(
                            "r",
                            encoding="utf-8-sig",
                            errors="replace",
                            newline="",
                        ) as handle:
                            reader = csv.reader(handle)
                            header = next(reader, [])
                            return [
                                str(item).strip()
                                for item in header
                                if str(item).strip()
                            ]
                    except Exception:
                        pass

            sql_text = (
                str(nl2sql.get("result_sql") or "").strip() if isinstance(nl2sql, dict) else ""
            )
            if not sql_text:
                return []

            aliases: list[str] = []
            for match in re.finditer(
                r"\bAS\s+(?:\"([^\"]+)\"|`([^`]+)`|((?!\d)\w+))",
                sql_text,
                flags=re.IGNORECASE | re.UNICODE,
            ):
                alias = str(match.group(1) or match.group(2) or match.group(3) or "").strip()
                if alias:
                    aliases.append(alias)
            return aliases

        available_ctes: list[dict[str, Any]] = []
        missing_units: list[dict[str, str]] = []

        for unit_result in unit_results:
            unit_name = str(unit_result.get("target_unit_name") or "").strip()
            unit_type = str(unit_result.get("target_unit_type") or "").strip()
            target_unit = unit_result.get("target_unit", {})
            nl2sql = unit_result.get("nl2sql", {})
            result_sql = (
                str(nl2sql.get("result_sql") or "").strip() if isinstance(nl2sql, dict) else ""
            )
            if not unit_name or not unit_type:
                continue

            safe_cte_name = to_safe_sql_symbol(unit_name, fallback=f"{unit_type}_cte")

            if not result_sql:
                missing_units.append(
                    {
                        "name": unit_name,
                        "cte_name": safe_cte_name,
                        "type": unit_type,
                        "reason": str(
                            nl2sql.get("error") or "NL2SQL did not produce SQL."
                        ).strip()
                        if isinstance(nl2sql, dict)
                        else "NL2SQL did not produce SQL.",
                    }
                )
                continue

            actual_columns = read_result_columns(unit_result)
            normalized_sql = result_sql.rstrip().rstrip(";")
            summary_key = "grain" if unit_type == "entity" else "meaning"
            summary_text = ""
            if isinstance(target_unit, dict):
                if unit_type == "entity":
                    summary_text = str(target_unit.get("grain") or target_unit.get("desc") or "").strip()
                else:
                    summary_text = str(target_unit.get("desc") or target_unit.get("grain") or "").strip()
            if not summary_text:
                summary_text = unit_name

            available_ctes.append(
                {
                    "name": safe_cte_name,
                    "source_unit_name": unit_name,
                    "type": unit_type,
                    summary_key: summary_text,
                    "columns": actual_columns,
                    "implement_sql": normalized_sql,
                    "cte_definition": "\n".join(
                        [
                            f"{render_identifier(safe_cte_name)} AS (",
                            indent_block(normalized_sql, "  "),
                            ")",
                        ]
                    ),
                }
            )

        mode = (
            "strict"
            if unit_results and len(available_ctes) == len(
                [item for item in unit_results if str(item.get("target_unit_name") or "").strip()]
            )
            else "lenient"
        )

        available_lines = ["Available CTEs:"]
        if available_ctes:
            for entry in available_ctes:
                available_lines.append(f"- {entry['name']}")
                if entry["source_unit_name"] != entry["name"]:
                    available_lines.append(f"  source_unit_name: {entry['source_unit_name']}")
                available_lines.append(f"  type: {entry['type']}")
                if entry["type"] == "entity":
                    available_lines.append(f"  grain: {entry['grain']}")
                else:
                    available_lines.append(f"  meaning: {entry['meaning']}")
                available_lines.append(
                    "  columns: "
                    + (", ".join(entry["columns"]) if entry["columns"] else "(none)")
                )
        else:
            available_lines.append("- (none)")
        available_ctes_text = "\n".join(available_lines)

        policy_lines = [
            "CTE Usage Policy:",
            f"- mode: {mode}",
            "- The system has already prepared the SQL implementations for the available CTE names above.",
            "- Do not repeat or redefine any available CTE.",
            "- Do not output a WITH clause unless you absolutely cannot avoid it.",
            "- Prefer to return a single query body that starts with SELECT.",
        ]
        if mode == "strict":
            policy_lines.extend(
                [
                    "- Strict mode: use only the available CTEs above.",
                    "- Strict mode: do not access underlying base tables or views.",
                ]
            )
        else:
            policy_lines.extend(
                [
                    "- Lenient mode: prefer the available CTEs above whenever possible.",
                    "- Lenient mode: if some ER units are unavailable, you may access underlying base tables or views directly when needed.",
                    "- If you access base tables or views, use fully qualified names whenever possible.",
                ]
            )
        policy_lines.append("- The execution layer will prepend the prepared CTE definitions for you.")
        policy_text = "\n".join(policy_lines)

        missing_units_text = ""
        if missing_units:
            missing_lines = ["Missing ER units:"]
            for item in missing_units:
                missing_lines.append(
                    f"- {item['cte_name']} ({item['type']}): {item['reason'] or 'unavailable'}"
                )
            missing_units_text = "\n".join(missing_lines)

        external_knowledge_sections: list[str] = []
        if resolved_external_knowledge:
            external_knowledge_sections.append(
                "Base external knowledge:\n" + resolved_external_knowledge
            )
        if resolved_db_hint:
            external_knowledge_sections.append("Database hint:\n" + resolved_db_hint)
        external_knowledge_sections.append(available_ctes_text)
        if missing_units_text:
            external_knowledge_sections.append(missing_units_text)
        external_knowledge_sections.append(policy_text)
        final_external_knowledge = "\n\n".join(
            section.strip() for section in external_knowledge_sections if section.strip()
        ).strip()

        mode_requirement = (
            "Because strict mode is active, do not access base tables or views."
            if mode == "strict"
            else (
                "Because lenient mode is active, you may access base tables or views only "
                "if the available CTEs are insufficient."
            )
        )
        prompt = self.prompt_builder.build_text(
            FINAL_QUERY_TEMPLATE_KEY,
            vars={
                "db_id": resolved_db_id or "(unknown)",
                "user_question": resolved_question,
                "external_knowledge": final_external_knowledge or "(none)",
                "response_mode": mode,
                "mode_requirement": mode_requirement,
            },
        ).strip()

        final_prompt_path = self.log_dir / "final_query_prompt.md"
        final_response_path = self.log_dir / "final_query_response.md"
        final_payload_path = self.log_dir / "final_query_result.json"
        final_external_knowledge_path = self.log_dir / "final_query_external_knowledge.txt"
        final_prompt_path.write_text(prompt, encoding="utf-8")
        final_external_knowledge_path.write_text(
            final_external_knowledge or "(none)",
            encoding="utf-8",
        )

        raw_response = self.question_llm.single_turn(
            prompt,
            system_prompt=(
                "You are a precise SQL planner. Return valid JSON only. "
                "Do not include markdown fences or explanations outside JSON."
            ),
            check_func=json_check,
            max_retry=max_retry,
        )
        final_response_path.write_text(raw_response or "", encoding="utf-8")

        parsed_response: dict[str, Any] = {}
        if raw_response and raw_response.strip():
            try:
                parsed_candidate = json_parse(raw_response)
                if isinstance(parsed_candidate, dict):
                    parsed_response = parsed_candidate
            except Exception:
                parsed_response = {}

        query_body = extract_sql_text(
            str(
                parsed_response.get("query_body")
                or parsed_response.get("sql")
                or parsed_response.get("query")
                or ""
            )
        )
        if not query_body:
            query_body = extract_sql_text(raw_response or "")
        query_body = query_body.rstrip(";").strip()

        with_clause = ""
        final_sql = ""
        assembly_mode = "failed"
        if available_ctes:
            with_clause = "WITH\n" + ",\n".join(entry["cte_definition"] for entry in available_ctes)

        if query_body:
            if re.match(r"^\s*WITH\b", query_body, flags=re.IGNORECASE):
                final_sql = query_body.rstrip(";").strip() + ";"
                assembly_mode = "model_provided_full_sql"
            elif with_clause:
                final_sql = with_clause + "\n" + query_body + ";"
                assembly_mode = "prepended_available_ctes"
            else:
                final_sql = query_body + ";"
                assembly_mode = "query_body_only"

        result_payload = {
            "ok": bool(final_sql),
            "mode": mode,
            "db_id": resolved_db_id,
            "user_question": resolved_question,
            "available_cte_count": len(available_ctes),
            "missing_unit_count": len(missing_units),
            "available_ctes_text": available_ctes_text,
            "available_ctes": available_ctes,
            "missing_units": missing_units,
            "external_knowledge": final_external_knowledge,
            "prompt_path": str(final_prompt_path),
            "external_knowledge_path": str(final_external_knowledge_path),
            "response_path": str(final_response_path),
            "raw_response": raw_response,
            "parsed_response": parsed_response,
            "query_body": query_body,
            "with_clause": with_clause,
            "final_sql": final_sql,
            "assembly_mode": assembly_mode,
        }
        write_json(final_payload_path, result_payload)
        result_payload["result_path"] = str(final_payload_path)
        return result_payload

    @staticmethod
    def build_analysis_schema_linking_json(unit_result: dict[str, Any]) -> str:
        schema_linking = unit_result.get("schema_linking", {})
        payload: dict[str, Any] = {
            "ok": None,
            "skipped": True,
            "tables": [],
            "columns": [],
            "error": None,
        }

        if isinstance(schema_linking, dict):
            payload["ok"] = schema_linking.get("ok")
            payload["skipped"] = bool(schema_linking.get("skipped", False))
            payload["error"] = schema_linking.get("error")

            tables: list[str] = []
            columns: list[str] = []

            for table in schema_linking.get("tables", []):
                if not isinstance(table, dict):
                    continue
                fullname = str(table.get("fullname", "")).strip()
                if fullname:
                    tables.append(fullname)

            for column in schema_linking.get("columns", []):
                if not isinstance(column, dict):
                    continue
                fullname = str(column.get("fullname", "")).strip()
                if fullname:
                    columns.append(fullname)

            payload["tables"] = tables
            payload["columns"] = columns

        return to_pretty_json(payload)

    @staticmethod
    def build_analysis_sql_result_evidence_json(unit_result: dict[str, Any]) -> str:
        payload: dict[str, Any] = {
            "result_columns": [],
            "sample_rows": [],
            "empty_or_zero_columns_in_sample": [],
        }

        nl2sql = unit_result.get("nl2sql", {})
        engine_result_path = (
            str(nl2sql.get("engine_result_path") or "").strip()
            if isinstance(nl2sql, dict)
            else ""
        )
        if not engine_result_path:
            return to_pretty_json(payload)

        try:
            engine_result = read_json(engine_result_path)
        except Exception:
            return to_pretty_json(payload)

        final_payload = engine_result.get("final", {})
        csv_path_value = (
            str(final_payload.get("csv_path") or "").strip()
            if isinstance(final_payload, dict)
            else ""
        )
        if not csv_path_value:
            return to_pretty_json(payload)

        csv_file = Path(csv_path_value)
        if not csv_file.exists():
            return to_pretty_json(payload)

        try:
            with csv_file.open(
                "r",
                encoding="utf-8-sig",
                errors="replace",
                newline="",
            ) as handle:
                reader = csv.DictReader(handle)
                fieldnames = [
                    str(name).strip()
                    for name in (reader.fieldnames or [])
                    if str(name).strip()
                ]
                payload["result_columns"] = fieldnames[:ANALYSIS_RESULT_SAMPLE_COLUMN_LIMIT]

                sample_rows: list[dict[str, str]] = []
                for row_index, row in enumerate(reader):
                    if row_index >= ANALYSIS_RESULT_SAMPLE_ROW_LIMIT:
                        break
                    if not isinstance(row, dict):
                        continue
                    sample_row: dict[str, str] = {}
                    for column_name in payload["result_columns"]:
                        value = row.get(column_name, "")
                        sample_row[column_name] = shorten_text(
                            "" if value is None else str(value),
                            limit=ANALYSIS_RESULT_SAMPLE_VALUE_MAX_CHARS,
                        )
                    if sample_row:
                        sample_rows.append(sample_row)

                payload["sample_rows"] = sample_rows

                empty_or_zero_columns: list[str] = []
                zero_like_values = {"", "0", "0.0", "null", "NULL", "None", "none"}
                for column_name in payload["result_columns"]:
                    values = [
                        str(sample_row.get(column_name, "")).strip()
                        for sample_row in sample_rows
                    ]
                    if values and all(value in zero_like_values for value in values):
                        empty_or_zero_columns.append(column_name)
                payload["empty_or_zero_columns_in_sample"] = empty_or_zero_columns
        except Exception:
            return to_pretty_json(payload)

        return to_pretty_json(payload)

    def build_analysis_target_definition(self, unit_result: dict[str, Any]) -> str:
        payload = {
            "target_unit_type": str(unit_result.get("target_unit_type") or ""),
            "target_unit_name": str(unit_result.get("target_unit_name") or ""),
            "target_unit": copy.deepcopy(unit_result.get("target_unit", {})),
        }
        if self.include_conditions_in_sql2nl:
            payload["applied_conditions"] = copy.deepcopy(
                unit_result.get("applied_conditions", [])
            )
        return to_pretty_json(payload)

    def build_target_semantics_summary(self, unit_result: dict[str, Any]) -> str:
        unit_type = str(unit_result.get("target_unit_type") or "unit").strip()
        unit_name = str(unit_result.get("target_unit_name") or "unknown_unit").strip()
        target_unit = unit_result.get("target_unit", {})
        desc = str(target_unit.get("desc") or "").strip() if isinstance(target_unit, dict) else ""

        conditions: list[str] = []
        if self.include_conditions_in_sql2nl:
            for condition in ensure_dict_list(unit_result.get("applied_conditions")):
                condition_text = str(
                    condition.get("condition_desc") or condition.get("name") or ""
                ).strip()
                if condition_text:
                    conditions.append(condition_text)

        parts = [desc or f"{unit_type} {unit_name} requires SQL support."]
        if conditions:
            parts.append("Relevant conditions: " + "; ".join(conditions))
        return " ".join(part for part in parts if part).strip()

    def build_analysis_prompt(
        self,
        unit_result: dict[str, Any],
        *,
        external_knowledge: str = "",
    ) -> str:
        nl2sql = unit_result.get("nl2sql", {})
        result_sql = (
            str(nl2sql.get("result_sql") or "")
            if isinstance(nl2sql, dict)
            else ""
        )
        return self.prompt_builder.build_text(
            ANALYSIS_TEMPLATE_KEY,
            vars={
                "unit_nl_query": str(unit_result.get("question") or ""),
                "external_knowledge": external_knowledge.strip() or "(none)",
                "target_unit_definition": self.build_analysis_target_definition(unit_result),
                "schema_linking_json": self.build_analysis_schema_linking_json(unit_result),
                "sql_result_evidence_json": self.build_analysis_sql_result_evidence_json(
                    unit_result
                ),
                "result_sql": result_sql,
            },
        )

    @staticmethod
    def validate_analysis_result(payload: dict[str, Any]) -> None:
        for field_name in ("unit_name", "target_semantics", "sql_semantics", "reason"):
            if not isinstance(payload.get(field_name), str):
                raise ValueError(f"Analysis payload field `{field_name}` must be a string.")

        verdict = payload.get("verdict")
        if verdict not in {"exact_match", "partial_match", "mismatch"}:
            raise ValueError("Analysis payload field `verdict` is invalid.")

        precision_check = payload.get("precision_check")
        if not isinstance(precision_check, dict):
            raise ValueError("Analysis payload field `precision_check` must be an object.")
        if not isinstance(precision_check.get("found_precisely"), bool):
            raise ValueError("Analysis payload precision_check.found_precisely must be a bool.")
        for field_name in ("matched_points", "shifted_or_incorrect_points"):
            if not isinstance(precision_check.get(field_name), str):
                raise ValueError(f"Analysis payload precision_check.{field_name} must be a string.")

        completeness_check = payload.get("completeness_check")
        if not isinstance(completeness_check, dict):
            raise ValueError("Analysis payload field `completeness_check` must be an object.")
        if not isinstance(completeness_check.get("found_completely"), bool):
            raise ValueError(
                "Analysis payload completeness_check.found_completely must be a bool."
            )
        for field_name in ("covered_points", "missing_points"):
            if not isinstance(completeness_check.get(field_name), str):
                raise ValueError(
                    f"Analysis payload completeness_check.{field_name} must be a string."
                )

    def build_placeholder_analysis(
        self,
        unit_result: dict[str, Any],
        reason: str,
        status: str,
    ) -> dict[str, Any]:
        unit_name = str(unit_result.get("target_unit_name") or "")
        nl2sql = unit_result.get("nl2sql", {})
        result_sql = (
            str(nl2sql.get("result_sql") or "")
            if isinstance(nl2sql, dict)
            else ""
        ).strip()

        if result_sql:
            sql_semantics = (
                "SQL was produced but could not be reliably analyzed. "
                f"SQL excerpt: {shorten_text(result_sql, limit=400)}"
            )
        else:
            sql_semantics = (
                "No SQL was produced for this unit, so there is no SQL semantics to evaluate."
            )

        return {
            "unit_name": unit_name,
            "target_semantics": self.build_target_semantics_summary(unit_result),
            "sql_semantics": sql_semantics,
            "precision_check": {
                "found_precisely": False,
                "matched_points": "",
                "shifted_or_incorrect_points": reason,
            },
            "completeness_check": {
                "found_completely": False,
                "covered_points": "",
                "missing_points": reason,
            },
            "verdict": "mismatch",
            "reason": reason,
            "analysis_status": status,
            "analysis_source": "deterministic_fallback",
        }

    def analyze_unit_sql_results(
        self,
        unit_results: list[dict[str, Any]],
        *,
        external_knowledge: str = "",
        max_concurrency: int | None = None,
    ) -> dict[str, Any]:
        output: dict[str, Any] = {}
        seen_unit_names: set[str] = set()
        runnable_units: list[dict[str, Any]] = []
        prompts: list[str] = []

        for unit_result in unit_results:
            unit_name = str(unit_result.get("target_unit_name", "")).strip()
            if not unit_name:
                continue
            if unit_name in seen_unit_names:
                raise ValueError(f"Duplicate target_unit_name detected in output: {unit_name}")
            seen_unit_names.add(unit_name)

            nl2sql = unit_result.get("nl2sql", {})
            result_sql = (
                str(nl2sql.get("result_sql") or "")
                if isinstance(nl2sql, dict)
                else ""
            ).strip()

            if not result_sql:
                output[unit_name] = self.build_placeholder_analysis(
                    unit_result,
                    reason="NL2SQL did not produce SQL.",
                    status="placeholder",
                )
                continue

            unit_type = str(unit_result.get("target_unit_type") or "").strip()
            analysis_external_knowledge = (
                external_knowledge if unit_type not in {"relationship", "connection"} else ""
            )
            prompt = self.build_analysis_prompt(
                unit_result,
                external_knowledge=analysis_external_knowledge,
            )
            prompt_path = self.analysis_prompt_dir / f"{safe_file_stem(str(unit_result['unit_id']))}.md"
            prompt_path.write_text(prompt, encoding="utf-8")
            prompts.append(prompt)
            runnable_units.append(unit_result)

        if not prompts:
            return output

        raw_responses = self.question_llm.batch_single_turn(
            prompts,
            check_func=json_check,
            max_concurrency=max_concurrency,
            show_progress=len(prompts) > 1,
            progress_desc="ER SQL analysis",
        )

        for unit_result, raw_response in zip(runnable_units, raw_responses):
            unit_name = str(unit_result.get("target_unit_name", "")).strip()
            response_path = (
                self.analysis_response_dir / f"{safe_file_stem(str(unit_result['unit_id']))}.md"
            )
            response_path.write_text(raw_response or "", encoding="utf-8")

            if not raw_response or not raw_response.strip():
                output[unit_name] = self.build_placeholder_analysis(
                    unit_result,
                    reason="Analysis LLM returned empty response.",
                    status="parse_error",
                )
                continue

            try:
                parsed = json_parse(raw_response)
                self.validate_analysis_result(parsed)
            except Exception as exc:
                output[unit_name] = self.build_placeholder_analysis(
                    unit_result,
                    reason=f"Analysis response could not be parsed: {exc}",
                    status="parse_error",
                )
                continue

            output[unit_name] = {
                "unit_name": unit_name,
                "target_semantics": parsed["target_semantics"].strip(),
                "sql_semantics": parsed["sql_semantics"].strip(),
                "precision_check": {
                    "found_precisely": parsed["precision_check"]["found_precisely"],
                    "matched_points": parsed["precision_check"]["matched_points"].strip(),
                    "shifted_or_incorrect_points": parsed["precision_check"][
                        "shifted_or_incorrect_points"
                    ].strip(),
                },
                "completeness_check": {
                    "found_completely": parsed["completeness_check"]["found_completely"],
                    "covered_points": parsed["completeness_check"]["covered_points"].strip(),
                    "missing_points": parsed["completeness_check"]["missing_points"].strip(),
                },
                "verdict": parsed["verdict"],
                "reason": parsed["reason"].strip(),
                "analysis_status": "ok",
                "analysis_source": "llm",
            }

        return output

    def run(
        self,
        *,
        input_path: str | Path,
        output_path: str | Path,
        db_id: str,
        max_question_concurrency: int | None,
        skip_schema_linking: bool,
        max_linking_workers: int,
        skip_nl2sql: bool,
        max_nl2sql_workers: int,
        engine_provider: str | None = None,
        spider2_root: str | Path | None = None,
        reforce_root: str | Path | None = None,
        engine_script: str | Path | None = None,
        nl2sql_engine_script: str | Path | None = None,
        schema_link_timeout_seconds: int | None = None,
        nl2sql_timeout_seconds: float = 600.0,
        schema_link_temperature: float = 0.0,
        shortlist_trigger: int = 18,
        max_shortlist_tables: int = 24,
        sample_row_limit: int = 2,
        sample_value_max_chars: int = 300,
        similar_tables_hint_limit: int = 12,
        external_knowledge: str = "",
        merge_connections_into_relationships: bool = False,
    ) -> dict[str, Any]:
        runtime = resolve_engine_runtime(
            provider_name=engine_provider,
            spider2_root=spider2_root,
            reforce_root=reforce_root,
            engine_script=engine_script,
            nl2sql_engine_script=nl2sql_engine_script,
        )
        er_model = normalize_er_input_payload(
            read_json(input_path),
            input_path=input_path,
            merge_connections_into_relationships=merge_connections_into_relationships,
        )
        resolved_external_knowledge = external_knowledge
        if not resolved_external_knowledge:
            candidate = er_model.get("external_knowledge")
            if isinstance(candidate, str):
                resolved_external_knowledge = candidate
        if not resolved_external_knowledge:
            resolved_external_knowledge = read_external_knowledge_from_sidecar(input_path)
        write_json(self.log_dir / "input.json", er_model)
        units = self.build_units(er_model)
        prompt_context = resolve_er2query_prompt_context(
            er_model=er_model,
            input_path=input_path,
            external_knowledge=resolved_external_knowledge,
        )

        if not units:
            if ensure_dict_list(er_model.get("connections")) and not merge_connections_into_relationships:
                raise ValueError(
                    "No `entities` or `relations` were found in the input ER JSON. "
                    "`connections` exist but `merge_connections_into_relationships` is disabled."
                )
            raise ValueError(
                "No `entities`, `relations`, or `connections` were found in the input ER JSON."
            )

        print(f"[ER2Data] loaded {len(units)} units from {Path(input_path)}")

        step_started_at = time.time()
        unit_results = self.generate_questions(
            units,
            prompt_context=prompt_context,
            max_concurrency=max_question_concurrency,
        )
        question_success_count = sum(
            1 for item in unit_results if item["question_generation"]["ok"]
        )
        emit_step_done_log(
            prefix="ER2Data",
            step="generate_questions",
            elapsed_seconds=time.time() - step_started_at,
            units=len(unit_results),
            questions=question_success_count,
        )

        if not skip_nl2sql:
            step_started_at = time.time()
            entity_indices = [
                idx
                for idx, item in enumerate(unit_results)
                if str(item.get("target_unit_type") or "").strip() == "entity"
            ]
            association_indices = [
                idx
                for idx, item in enumerate(unit_results)
                if str(item.get("target_unit_type") or "").strip() in {"relationship", "connection"}
            ]

            self.run_nl2sql_batch(
                unit_results,
                db_id=db_id,
                target_indices=entity_indices,
                external_knowledge_resolver=lambda unit_result: self.build_entity_external_knowledge(
                    unit_result,
                    base_external_knowledge=resolved_external_knowledge,
                ),
                max_workers=max_nl2sql_workers,
                provider_name=runtime.provider_name,
                runtime=runtime,
                timeout_seconds=nl2sql_timeout_seconds,
            )
            entity_sql_by_name = self.collect_successful_entity_sql(unit_results)
            self.run_nl2sql_batch(
                unit_results,
                db_id=db_id,
                target_indices=association_indices,
                external_knowledge_resolver=lambda unit_result: self.build_relationship_external_knowledge(
                    unit_result,
                    base_external_knowledge=resolved_external_knowledge,
                    entity_sql_by_name=entity_sql_by_name,
                ),
                max_workers=max_nl2sql_workers,
                provider_name=runtime.provider_name,
                runtime=runtime,
                timeout_seconds=nl2sql_timeout_seconds,
            )
        else:
            emit_step_done_log(
                prefix="ER2Data",
                step="skip_nl2sql",
                elapsed_seconds=0.0,
                units=question_success_count,
                requested=True,
            )

        nl2sql_success_count = sum(
            1
            for item in unit_results
            if isinstance(item.get("nl2sql"), dict) and item["nl2sql"].get("ok")
        )
        schema_link_success_count = sum(
            1
            for item in unit_results
            if isinstance(item.get("schema_linking"), dict) and item["schema_linking"].get("ok")
        )
        if not skip_nl2sql:
            emit_step_done_log(
                prefix="ER2Data",
                step="run_nl2sql_batch",
                elapsed_seconds=time.time() - step_started_at,
                units=question_success_count,
                workers=max(1, max_nl2sql_workers),
                sql_ok=nl2sql_success_count,
                schema_link_ok=schema_link_success_count,
            )

        output_path = Path(output_path)
        analysis_output_path = output_path.with_name(DEFAULT_ANALYSIS_FILENAME)
        final_query_output_path = output_path.with_name(DEFAULT_FINAL_QUERY_FILENAME)
        minimal_payload = self.build_minimal_output(unit_results)
        write_json(output_path, minimal_payload)
        step_started_at = time.time()
        analysis_payload = self.analyze_unit_sql_results(
            unit_results,
            external_knowledge=resolved_external_knowledge,
            max_concurrency=max_question_concurrency,
        )
        analysis_count = len(analysis_payload)
        analysis_llm_count = sum(
            1
            for item in analysis_payload.values()
            if isinstance(item, dict) and item.get("analysis_source") == "llm"
        )
        analysis_placeholder_count = analysis_count - analysis_llm_count
        emit_step_done_log(
            prefix="ER2Data",
            step="analyze_sql_results",
            elapsed_seconds=time.time() - step_started_at,
            analyses=analysis_count,
            llm=analysis_llm_count,
            placeholders=analysis_placeholder_count,
        )
        write_json(analysis_output_path, analysis_payload)
        step_started_at = time.time()
        final_query_payload = self.build_final_query_from_available_ctes(
            unit_results=unit_results,
            user_question=str(er_model.get("user_intent") or er_model.get("instruction") or "").strip(),
            input_path=input_path,
            db_id=db_id,
            base_external_knowledge=resolved_external_knowledge,
        )
        write_json(final_query_output_path, final_query_payload)
        emit_step_done_log(
            prefix="ER2Data",
            step="build_final_query",
            elapsed_seconds=time.time() - step_started_at,
            ok=bool(final_query_payload.get("ok")),
            mode=str(final_query_payload.get("mode") or ""),
            available_ctes=int(final_query_payload.get("available_cte_count") or 0),
            missing_units=int(final_query_payload.get("missing_unit_count") or 0),
        )

        return {
            "output_path": str(output_path),
            "analysis_output_path": str(analysis_output_path),
            "final_query_output_path": str(final_query_output_path),
            "log_dir": str(self.log_dir),
            "unit_count": len(unit_results),
            "question_success_count": question_success_count,
            "schema_link_success_count": schema_link_success_count,
            "nl2sql_success_count": nl2sql_success_count,
            "analysis_count": analysis_count,
            "analysis_llm_count": analysis_llm_count,
            "analysis_placeholder_count": analysis_placeholder_count,
            "final_query_ok": bool(final_query_payload.get("ok")),
            "final_query_mode": str(final_query_payload.get("mode") or ""),
            "final_query_available_cte_count": int(
                final_query_payload.get("available_cte_count") or 0
            ),
            "final_query_missing_unit_count": int(
                final_query_payload.get("missing_unit_count") or 0
            ),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Convert ER semantic units into natural-language questions and run schema linking in parallel."
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--db-id", default=DEFAULT_DB_ID)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    parser.add_argument("--question-model-config", default=None)
    parser.add_argument("--schema-link-model-config", default=None)
    parser.add_argument("--nl2sql-model-config", default=None)
    parser.add_argument(
        "--er2query-template-name",
        default=DEFAULT_ER2QUERY_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--exclude-desc-in-er2query",
        action="store_true",
        help="Do not pass target-unit desc into the ER2Query prompt.",
    )
    parser.add_argument("--include-conditions-in-er2query", action="store_true")
    parser.add_argument("--include-conditions-in-sql2nl", action="store_true")
    parser.add_argument("--max-question-concurrency", type=int, default=None)
    parser.add_argument("--skip-schema-linking", action="store_true")
    parser.add_argument("--max-linking-workers", type=int, default=4)
    parser.add_argument("--skip-nl2sql", action="store_true")
    parser.add_argument("--max-nl2sql-workers", type=int, default=1)
    parser.add_argument("--engine-provider", default=None)
    parser.add_argument("--spider2-root", type=Path, default=None)
    parser.add_argument("--reforce-root", type=Path, default=None)
    parser.add_argument("--engine-script", type=Path, default=None)
    parser.add_argument("--nl2sql-engine-script", type=Path, default=None)
    parser.add_argument("--schema-link-timeout-seconds", type=int, default=None)
    parser.add_argument("--nl2sql-timeout-seconds", type=float, default=600.0)
    parser.add_argument("--schema-link-temperature", type=float, default=0.0)
    parser.add_argument("--shortlist-trigger", type=int, default=18)
    parser.add_argument("--max-shortlist-tables", type=int, default=24)
    parser.add_argument("--sample-row-limit", type=int, default=2)
    parser.add_argument("--sample-value-max-chars", type=int, default=300)
    parser.add_argument("--similar-tables-hint-limit", type=int, default=12)
    parser.add_argument("--merge-connections-into-relationships", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    sidecar_context = read_sidecar_context(args.input_path)
    question_id = str(args.question_id or read_question_id(args.input_path)).strip()
    if not question_id:
        raise ValueError(
            "`question_id` is required. Provide it in the input JSON or pass --question-id."
        )
    db_id = str(args.db_id or "").strip()
    if not cli_option_provided("db-id") and (not db_id or db_id == DEFAULT_DB_ID):
        sidecar_db_id = sidecar_context.get("db_id", "").strip()
        if sidecar_db_id:
            db_id = sidecar_db_id
    if not db_id:
        db_id = DEFAULT_DB_ID

    runtime_preview = resolve_engine_runtime(
        provider_name=args.engine_provider,
        spider2_root=args.spider2_root,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
        nl2sql_engine_script=args.nl2sql_engine_script,
    )
    database_source = None
    database_root = None
    database_resolution_error = None
    try:
        database_resolution = resolve_database_resource(
            db_id,
            spider2_root=runtime_preview.spider2_root or DEFAULT_SPIDER2_ROOT,
        )
    except Exception as exc:
        database_resolution_error = str(exc)
    else:
        database_source = database_resolution.source
        database_root = str(database_resolution.db_root)

    run_timestamp = build_timestamp()
    run_id = f"{question_id}_{run_timestamp}"
    log_dir = resolve_run_log_dir(
        run_prefix=question_id,
        log_dir=args.log_dir,
        log_root=args.log_root,
        timestamp=run_timestamp,
    )
    metadata_dir = resolve_run_dir(
        run_prefix=question_id,
        run_dir=args.metadata_dir,
        run_root=args.metadata_root,
        timestamp=run_timestamp,
    )
    output_path = resolve_output_path(
        output_path=args.output_path,
        run_dir=metadata_dir,
        default_filename=DEFAULT_OUTPUT_FILENAME,
    )
    final_query_output_path = output_path.with_name(DEFAULT_FINAL_QUERY_FILENAME)

    write_json(
        metadata_dir / "input.json",
        {
            "question_id": question_id,
            "db_id": db_id,
            "input_path": str(args.input_path),
        },
    )
    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": question_id,
            "db_id": db_id,
            "timestamp": run_timestamp,
            "input_path": str(args.input_path),
            "metadata_dir": str(metadata_dir),
            "log_dir": str(log_dir),
            "output_path": str(output_path),
            "final_query_output_path": str(final_query_output_path),
            "engine_provider": args.engine_provider,
            "database_source": database_source,
            "database_root": database_root,
            "database_resolution_error": database_resolution_error,
            "er2query_template_name": args.er2query_template_name,
            "include_desc_in_er2query": not args.exclude_desc_in_er2query,
            "include_conditions_in_er2query": args.include_conditions_in_er2query,
            "include_conditions_in_sql2nl": args.include_conditions_in_sql2nl,
            "merge_connections_into_relationships": args.merge_connections_into_relationships,
        },
    )

    print(f"[ER2Data] run_id={run_id}")
    print(f"[ER2Data] question_id={question_id}")
    print(f"[ER2Data] db_id={db_id}")
    if database_source and database_root:
        print(f"[ER2Data] database_source={database_source}")
        print(f"[ER2Data] database_root={database_root}")
    elif database_resolution_error:
        print(f"[ER2Data] database_resolution_error={database_resolution_error}")

    runner = ER2DataRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        er2query_template_name=args.er2query_template_name,
        question_model_config=args.question_model_config,
        schema_link_model_config=args.schema_link_model_config,
        nl2sql_model_config=args.nl2sql_model_config,
        include_desc_in_er2query=not args.exclude_desc_in_er2query,
        include_conditions_in_er2query=args.include_conditions_in_er2query,
        include_conditions_in_sql2nl=args.include_conditions_in_sql2nl,
    )

    payload = runner.run(
        input_path=args.input_path,
        output_path=output_path,
        db_id=db_id,
        max_question_concurrency=args.max_question_concurrency,
        skip_schema_linking=args.skip_schema_linking,
        max_linking_workers=args.max_linking_workers,
        skip_nl2sql=args.skip_nl2sql,
        max_nl2sql_workers=args.max_nl2sql_workers,
        engine_provider=args.engine_provider,
        spider2_root=args.spider2_root,
        reforce_root=args.reforce_root,
        engine_script=args.engine_script,
        nl2sql_engine_script=args.nl2sql_engine_script,
        schema_link_timeout_seconds=args.schema_link_timeout_seconds,
        nl2sql_timeout_seconds=args.nl2sql_timeout_seconds,
        schema_link_temperature=args.schema_link_temperature,
        shortlist_trigger=args.shortlist_trigger,
        max_shortlist_tables=args.max_shortlist_tables,
        sample_row_limit=args.sample_row_limit,
        sample_value_max_chars=args.sample_value_max_chars,
        similar_tables_hint_limit=args.similar_tables_hint_limit,
        merge_connections_into_relationships=args.merge_connections_into_relationships,
    )

    write_json(
        metadata_dir / "run_context.json",
        {
            "run_id": run_id,
            "question_id": question_id,
            "db_id": db_id,
            "timestamp": run_timestamp,
            "input_path": str(args.input_path),
            "metadata_dir": str(metadata_dir),
            "log_dir": str(log_dir),
            "output_path": str(payload["output_path"]),
            "analysis_output_path": str(payload["analysis_output_path"]),
            "final_query_output_path": str(payload["final_query_output_path"]),
            "engine_provider": args.engine_provider,
            "database_source": database_source,
            "database_root": database_root,
            "database_resolution_error": database_resolution_error,
            "er2query_template_name": args.er2query_template_name,
            "include_desc_in_er2query": not args.exclude_desc_in_er2query,
            "include_conditions_in_er2query": args.include_conditions_in_er2query,
            "include_conditions_in_sql2nl": args.include_conditions_in_sql2nl,
            "merge_connections_into_relationships": args.merge_connections_into_relationships,
            "er2data_summary": payload,
        },
    )

    print(
        "[ER2Data] done: "
        f"{payload['question_success_count']} questions, "
        f"{payload['schema_link_success_count']} successful schema linking runs, "
        f"{payload['nl2sql_success_count']} successful NL2SQL runs"
    )
    print(f"[ER2Data] output wrote to {payload['output_path']}")
    print(f"[ER2Data] analysis wrote to {payload['analysis_output_path']}")
    print(f"[ER2Data] final query wrote to {payload['final_query_output_path']}")
    print(f"[ER2Data] logs wrote to {payload['log_dir']}")
    print(f"[ER2Data] metadata wrote to {metadata_dir}")


if __name__ == "__main__":
    main()
