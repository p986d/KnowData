from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import apply_reasoning_mode
from src.nl2sql.db_resource_locator import resolve_database_resource
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse

DEFAULT_PROMPT_DIR = Path("src/prompt/prompt_template")
DEFAULT_PROMPT_TEMPLATE_NAME = "ER2Data_selection_test_v1.md"
DEFAULT_SAMPLE_ROW_LIMIT = 2
DEFAULT_SAMPLE_VALUE_MAX_CHARS = 160
BINARY_SAMPLE_PLACEHOLDER = "<binary_value_omitted>"


def _normalize_identifier(value: str) -> str:
    return str(value or "").strip().replace('"', "").replace("`", "").upper()


def _normalize_qualified_name(value: str) -> str:
    parts = [_normalize_identifier(part) for part in str(value or "").split(".")]
    return ".".join(part for part in parts if part)


def _split_qualified_parts(value: str) -> tuple[str, ...]:
    normalized = _normalize_qualified_name(value)
    if not normalized:
        return ()
    return tuple(part for part in normalized.split(".") if part)


def _names_match_by_suffix(left: str, right: str) -> bool:
    left_parts = _split_qualified_parts(left)
    right_parts = _split_qualified_parts(right)
    if not left_parts or not right_parts:
        return False
    shorter, longer = (
        (left_parts, right_parts)
        if len(left_parts) <= len(right_parts)
        else (right_parts, left_parts)
    )
    return tuple(longer[-len(shorter) :]) == tuple(shorter)


def _extract_attribute_names(raw_attributes: Any) -> list[str]:
    if not isinstance(raw_attributes, list):
        return []
    names: list[str] = []
    for item in raw_attributes:
        if isinstance(item, dict):
            name = str(item.get("name") or "").strip()
        else:
            name = str(item or "").strip()
        if name:
            names.append(name)
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        normalized = _normalize_identifier(name)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        unique.append(name)
    return unique


def _shorten_text(text: str, max_chars: int) -> str:
    if max_chars < 1:
        return ""
    if len(text) <= max_chars:
        return text
    if max_chars <= 3:
        return text[:max_chars]
    return text[: max_chars - 3] + "..."


def _sanitize_sample_value(value: Any, *, max_chars: int) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, (bytes, bytearray)):
        return BINARY_SAMPLE_PLACEHOLDER
    text = str(value)
    if "bytearray(b" in text or text.startswith("b'") or text.startswith('b"'):
        return BINARY_SAMPLE_PLACEHOLDER
    return _shorten_text(text, max_chars)


def build_entity_candidates(er_model: dict[str, Any]) -> list[dict[str, Any]]:
    entities = er_model.get("entities")
    if not isinstance(entities, list):
        return []
    candidates: list[dict[str, Any]] = []
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        name = str(entity.get("name") or "").strip()
        if not name:
            continue
        identifier_attrs = _extract_attribute_names(entity.get("identifier_attrs") or [])
        attrs = _extract_attribute_names(entity.get("attrs") or [])
        candidates.append(
            {
                "name": name,
                "desc": str(entity.get("desc") or "").strip(),
                "grain": str(entity.get("grain") or "").strip(),
                "identifier_attrs": identifier_attrs,
                "attrs": attrs,
            }
        )
    return candidates


def _normalize_participants(raw_participants: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_participants, list):
        return []
    participants: list[dict[str, Any]] = []
    for item in raw_participants:
        if not isinstance(item, dict):
            continue
        entity = str(item.get("entity") or "").strip()
        role = str(item.get("role") or "").strip()
        if not entity:
            continue
        payload: dict[str, Any] = {"entity": entity}
        if role:
            payload["role"] = role
        identifier_attrs = _merge_unique_strings(
            list(item.get("identifier_attrs") or []),
            list(item.get("anchor_attribute") or []),
        )
        if identifier_attrs:
            payload["identifier_attrs"] = identifier_attrs
        participants.append(payload)
    return participants


def build_relation_candidates(er_model: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source_key, unit_type in (("relations", "relation"), ("connections", "connection")):
        raw_units = er_model.get(source_key)
        if not isinstance(raw_units, list):
            continue
        for item in raw_units:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            key = f"{unit_type}:{_normalize_identifier(name)}"
            if key in seen:
                continue
            seen.add(key)
            attrs = _merge_attr_payloads(list(item.get("attrs") or []), [])
            payload = {
                "name": name,
                "unit_type": unit_type,
                "relation_type": str(item.get("relation_type") or unit_type).strip(),
                "desc": str(item.get("desc") or item.get("link_condition") or "").strip(),
                "grain": str(item.get("grain") or "").strip(),
                "participants": _normalize_participants(item.get("participants")),
                "attrs": attrs,
            }
            link_condition = str(item.get("link_condition") or "").strip()
            if link_condition:
                payload["link_condition"] = link_condition
            candidates.append(payload)
    return candidates


def _merge_unique_strings(left: list[Any], right: list[Any]) -> list[str]:
    output: list[str] = []
    seen: set[str] = set()
    for value in [*left, *right]:
        text = str(value or "").strip()
        if not text:
            continue
        key = _normalize_identifier(text)
        if key in seen:
            continue
        seen.add(key)
        output.append(text)
    return output


def _merge_attr_payloads(left: list[Any], right: list[Any]) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}

    for item in [*left, *right]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = _normalize_identifier(name)
        existing_index = index_by_name.get(key)
        if existing_index is None:
            index_by_name[key] = len(output)
            output.append(copy.deepcopy(item))
            continue

        existing = output[existing_index]
        for field in ("semantics", "desc", "description"):
            if not str(existing.get(field) or "").strip() and str(item.get(field) or "").strip():
                existing[field] = str(item.get(field) or "").strip()

    return output


def _merge_entity_payload(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(left)
    for field in ("desc", "grain", "role", "source_type"):
        if not str(merged.get(field) or "").strip() and str(right.get(field) or "").strip():
            merged[field] = str(right.get(field) or "").strip()
    merged["identifier_attrs"] = _merge_unique_strings(
        list(merged.get("identifier_attrs") or []),
        list(right.get("identifier_attrs") or []),
    )
    merged["attrs"] = _merge_attr_payloads(
        list(merged.get("attrs") or []),
        list(right.get("attrs") or []),
    )
    return merged


def _merge_named_list(
    left: list[Any],
    right: list[Any],
    *,
    merge_entities: bool = False,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    index_by_name: dict[str, int] = {}
    for item in [*left, *right]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            output.append(copy.deepcopy(item))
            continue
        key = _normalize_identifier(name)
        existing_index = index_by_name.get(key)
        if existing_index is None:
            index_by_name[key] = len(output)
            output.append(copy.deepcopy(item))
            continue
        if merge_entities:
            output[existing_index] = _merge_entity_payload(output[existing_index], item)
    return output


def merge_er_models(*models: dict[str, Any]) -> dict[str, Any]:
    """Merge primary and diff ER models for ER-selection candidate construction."""
    normalized_models = [model for model in models if isinstance(model, dict)]
    if not normalized_models:
        return {}

    merged = copy.deepcopy(normalized_models[0])
    for model in normalized_models[1:]:
        merged["entities"] = _merge_named_list(
            list(merged.get("entities") or []),
            list(model.get("entities") or []),
            merge_entities=True,
        )
        merged["relations"] = _merge_named_list(
            list(merged.get("relations") or []),
            list(model.get("relations") or []),
        )
        merged["connections"] = _merge_named_list(
            list(merged.get("connections") or []),
            list(model.get("connections") or []),
        )
        merged["conditions"] = _merge_named_list(
            list(merged.get("conditions") or []),
            list(model.get("conditions") or []),
        )
    return merged


def _load_table_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    if not payload.get("table_fullname") and not payload.get("table_name"):
        return None
    if not isinstance(payload.get("column_names"), list):
        return None
    return payload


def _select_related_table_snapshots(
    *,
    db_root: Path,
    linked_tables: list[str],
    linked_columns: list[str],
    sample_row_limit: int = DEFAULT_SAMPLE_ROW_LIMIT,
    sample_value_max_chars: int = DEFAULT_SAMPLE_VALUE_MAX_CHARS,
) -> list[dict[str, Any]]:
    normalized_linked_tables = {
        _normalize_qualified_name(item)
        for item in linked_tables
        if _normalize_qualified_name(item)
    }
    normalized_linked_columns = {
        _normalize_qualified_name(item)
        for item in linked_columns
        if _normalize_qualified_name(item)
    }
    linked_column_names = {
        _normalize_identifier(item.rsplit(".", 1)[-1])
        for item in normalized_linked_columns
        if "." in item
    }

    snapshots: list[dict[str, Any]] = []
    for path in sorted(db_root.rglob("*.json")):
        snapshot = _load_table_snapshot(path)
        if snapshot is None:
            continue

        table_fullname = _normalize_qualified_name(str(snapshot.get("table_fullname") or ""))
        table_name = _normalize_qualified_name(str(snapshot.get("table_name") or ""))
        if not table_fullname and not table_name:
            continue

        table_match = any(
            _names_match_by_suffix(candidate, table_fullname or table_name)
            or _names_match_by_suffix(candidate, table_name or table_fullname)
            for candidate in normalized_linked_tables
        )
        if not table_match:
            continue

        column_names = snapshot.get("column_names") or []
        column_types = snapshot.get("column_types") or []
        descriptions = snapshot.get("description") or []
        linked_column_names_for_table: list[str] = []
        column_payloads: list[dict[str, Any]] = []
        for index, column_name_raw in enumerate(column_names):
            column_name = str(column_name_raw or "").strip()
            if not column_name:
                continue
            normalized_column_name = _normalize_identifier(column_name)
            full_candidates = [
                _normalize_qualified_name(f"{table_fullname}.{column_name}")
                if table_fullname
                else "",
                _normalize_qualified_name(f"{table_name}.{column_name}") if table_name else "",
            ]
            is_linked_column = (
                normalized_column_name in linked_column_names
                or any(
                    candidate
                    and any(
                        _names_match_by_suffix(candidate, linked_column)
                        for linked_column in normalized_linked_columns
                    )
                    for candidate in full_candidates
                )
            )
            if is_linked_column:
                linked_column_names_for_table.append(column_name)
                column_payload = {"name": column_name}
                if index < len(column_types):
                    column_payload["type"] = str(column_types[index] or "").strip()
                if index < len(descriptions):
                    column_payload["description"] = str(descriptions[index] or "").strip()
                column_payloads.append(column_payload)

        linked_column_name_set = set(linked_column_names_for_table)
        sample_rows: list[dict[str, Any]] = []
        for row in list(snapshot.get("sample_rows") or [])[: max(0, sample_row_limit)]:
            if not isinstance(row, dict):
                continue
            projected_row = {
                column_name: _sanitize_sample_value(
                    row.get(column_name),
                    max_chars=sample_value_max_chars,
                )
                for column_name in linked_column_names_for_table
                if column_name in row
            }
            if projected_row:
                sample_rows.append(projected_row)

        snapshots.append(
            {
                "table_name": str(snapshot.get("table_name") or "").strip(),
                "table_fullname": str(snapshot.get("table_fullname") or "").strip(),
                "columns": [
                    column
                    for column in column_payloads
                    if str(column.get("name") or "").strip() in linked_column_name_set
                ],
                "sample_rows": sample_rows,
            }
        )
    return snapshots


def build_schema_snapshot(
    *,
    db_id: str,
    linked_tables: list[str],
    linked_columns: list[str],
    spider2_root: str | Path,
    sample_row_limit: int = DEFAULT_SAMPLE_ROW_LIMIT,
    sample_value_max_chars: int = DEFAULT_SAMPLE_VALUE_MAX_CHARS,
) -> dict[str, Any]:
    resource = resolve_database_resource(
        db_id=db_id,
        spider2_root=spider2_root,
    )
    table_snapshots = _select_related_table_snapshots(
        db_root=resource.db_root,
        linked_tables=linked_tables,
        linked_columns=linked_columns,
        sample_row_limit=sample_row_limit,
        sample_value_max_chars=sample_value_max_chars,
    )
    return {
        "db_id": db_id,
        "table_count": len(table_snapshots),
        "tables": table_snapshots,
    }


def _build_prompt(
    *,
    question: str,
    external_knowledge: str = "",
    schema_snapshot: dict[str, Any],
    entity_candidates: list[dict[str, Any]],
    relation_candidates: list[dict[str, Any]] | None = None,
    include_relation_candidates: bool = False,
    prompt_dir: str | Path = DEFAULT_PROMPT_DIR,
    prompt_template_name: str = DEFAULT_PROMPT_TEMPLATE_NAME,
) -> str:
    prompt_builder = PromptBuilder(
        template_dir=prompt_dir,
        strict_undefined=True,
    )
    prompt_builder.register_template(
        name="er_selection",
        template_name=prompt_template_name,
        required_vars=["question", "schema_snapshot", "entity_candidates"],
        default_vars={
            "external_knowledge": "",
            "relation_candidates": [],
            "include_relation_candidates": False,
        },
        description="Select ER entities and attributes from schema evidence.",
    )
    return prompt_builder.build_text(
        "er_selection",
        vars={
            "question": question,
            "external_knowledge": external_knowledge,
            "schema_snapshot": schema_snapshot,
            "entity_candidates": entity_candidates,
            "relation_candidates": relation_candidates or [],
            "include_relation_candidates": include_relation_candidates,
        },
    )


def _sanitize_selection_payload(
    payload: dict[str, Any],
    *,
    entity_candidates: list[dict[str, Any]],
    relation_candidates: list[dict[str, Any]] | None = None,
    include_relation_candidates: bool = False,
) -> dict[str, Any]:
    by_name = {str(item["name"]).strip(): item for item in entity_candidates}
    selected_entities_raw = payload.get("entities")
    if not isinstance(selected_entities_raw, list):
        selected_entities_raw = payload.get("selected_entities")
    if not isinstance(selected_entities_raw, list):
        selected_entities_raw = []

    entities: list[dict[str, Any]] = []
    for item in selected_entities_raw:
        if not isinstance(item, dict):
            continue
        entity_name = str(item.get("name") or item.get("entity_name") or "").strip()
        if not entity_name:
            continue

        entity_def = by_name.get(entity_name, {})
        valid_attrs = {
            _normalize_identifier(attr)
            for attr in [
                *entity_def.get("identifier_attrs", []),
                *entity_def.get("attrs", []),
            ]
        }
        selected_attrs_raw = item.get("attributes")
        if not isinstance(selected_attrs_raw, list):
            selected_attrs_raw = item.get("selected_attributes")
        attributes: list[dict[str, Any]] = []
        if isinstance(selected_attrs_raw, list):
            for attr in selected_attrs_raw:
                if isinstance(attr, dict):
                    attr_name = str(attr.get("name") or "").strip()
                    semantics = str(attr.get("semantics") or "").strip()
                    evidence_columns = [
                        str(value).strip()
                        for value in list(attr.get("evidence_columns") or [])
                        if str(value or "").strip()
                    ]
                else:
                    attr_name = str(attr or "").strip()
                    semantics = ""
                    evidence_columns = []
                if not attr_name:
                    continue
                if valid_attrs and _normalize_identifier(attr_name) not in valid_attrs:
                    continue
                attr_payload = {
                    "name": attr_name,
                    "semantics": semantics,
                }
                if evidence_columns:
                    attr_payload["evidence_columns"] = evidence_columns
                attributes.append(attr_payload)

        entities.append(
            {
                "name": entity_name,
                "desc": str(
                    item.get("desc")
                    or item.get("description")
                    or entity_def.get("desc")
                    or ""
                ).strip(),
                "grain": str(item.get("grain") or entity_def.get("grain") or "").strip(),
                "attributes": attributes,
                "evidence_tables": [
                    str(value).strip()
                    for value in list(item.get("evidence_tables") or [])
                    if str(value or "").strip()
                ],
                "evidence_columns": [
                    str(value).strip()
                    for value in list(item.get("evidence_columns") or [])
                    if str(value or "").strip()
                ],
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    selection_payload: dict[str, Any] = {
        "entities": entities,
        "selected_entities": [
            {
                "entity_name": entity["name"],
                "selected_attributes": [
                    attribute["name"]
                    for attribute in entity.get("attributes", [])
                    if str(attribute.get("name") or "").strip()
                ],
                "evidence_tables": list(entity.get("evidence_tables") or []),
                "evidence_columns": list(entity.get("evidence_columns") or []),
                "reason": str(entity.get("reason") or ""),
            }
            for entity in entities
        ],
    }
    if include_relation_candidates:
        selection_payload.update(
            _sanitize_relation_selection_payload(
                payload,
                relation_candidates=relation_candidates or [],
            )
        )
    return selection_payload


def _sanitize_relation_selection_payload(
    payload: dict[str, Any],
    *,
    relation_candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    by_name = {str(item["name"]).strip(): item for item in relation_candidates}
    selected_relations_raw = payload.get("relations")
    if not isinstance(selected_relations_raw, list):
        selected_relations_raw = payload.get("selected_relations")
    if not isinstance(selected_relations_raw, list):
        selected_relations_raw = []

    relations: list[dict[str, Any]] = []
    for item in selected_relations_raw:
        if not isinstance(item, dict):
            continue
        relation_name = str(item.get("name") or item.get("relation_name") or "").strip()
        if not relation_name:
            continue
        relation_def = by_name.get(relation_name, {})
        valid_attrs = {
            _normalize_identifier(attr.get("name") or "")
            for attr in list(relation_def.get("attrs") or [])
            if isinstance(attr, dict)
        }
        selected_attrs_raw = item.get("attributes")
        if not isinstance(selected_attrs_raw, list):
            selected_attrs_raw = item.get("selected_attributes")
        attributes: list[dict[str, Any]] = []
        if isinstance(selected_attrs_raw, list):
            for attr in selected_attrs_raw:
                if isinstance(attr, dict):
                    attr_name = str(attr.get("name") or "").strip()
                    semantics = str(attr.get("semantics") or "").strip()
                    evidence_columns = [
                        str(value).strip()
                        for value in list(attr.get("evidence_columns") or [])
                        if str(value or "").strip()
                    ]
                else:
                    attr_name = str(attr or "").strip()
                    semantics = ""
                    evidence_columns = []
                if not attr_name:
                    continue
                if valid_attrs and _normalize_identifier(attr_name) not in valid_attrs:
                    continue
                attr_payload = {"name": attr_name, "semantics": semantics}
                if evidence_columns:
                    attr_payload["evidence_columns"] = evidence_columns
                attributes.append(attr_payload)

        participants = item.get("participants")
        if not isinstance(participants, list):
            participants = relation_def.get("participants") or []
        relations.append(
            {
                "name": relation_name,
                "unit_type": str(item.get("unit_type") or relation_def.get("unit_type") or "relation"),
                "relation_type": str(
                    item.get("relation_type") or relation_def.get("relation_type") or ""
                ).strip(),
                "desc": str(
                    item.get("desc")
                    or item.get("description")
                    or relation_def.get("desc")
                    or ""
                ).strip(),
                "grain": str(item.get("grain") or relation_def.get("grain") or "").strip(),
                "participants": _normalize_participants(participants),
                "attributes": attributes,
                "evidence_tables": [
                    str(value).strip()
                    for value in list(item.get("evidence_tables") or [])
                    if str(value or "").strip()
                ],
                "evidence_columns": [
                    str(value).strip()
                    for value in list(item.get("evidence_columns") or [])
                    if str(value or "").strip()
                ],
                "reason": str(item.get("reason") or "").strip(),
            }
        )
    return {
        "relations": relations,
        "selected_relations": [
            {
                "relation_name": relation["name"],
                "selected_attributes": [
                    attribute["name"]
                    for attribute in relation.get("attributes", [])
                    if str(attribute.get("name") or "").strip()
                ],
                "participants": list(relation.get("participants") or []),
                "reason": str(relation.get("reason") or ""),
            }
            for relation in relations
        ],
    }


def select_entities_with_llm(
    *,
    question: str,
    external_knowledge: str = "",
    schema_snapshot: dict[str, Any],
    entity_candidates: list[dict[str, Any]],
    relation_candidates: list[dict[str, Any]] | None = None,
    include_relation_candidates: bool = False,
    model_config_name: str | None,
    reasoning_mode: str | None = None,
    prompt_dir: str | Path = DEFAULT_PROMPT_DIR,
    prompt_template_name: str = DEFAULT_PROMPT_TEMPLATE_NAME,
) -> dict[str, Any]:
    settings = load_settings()
    resolved_model_config_name = model_config_name or settings.llm.default_model
    llm_config = apply_reasoning_mode(
        settings.llm.get(resolved_model_config_name),
        reasoning_mode,
    )
    llm = LLMClient(llm_config)
    prompt = _build_prompt(
        question=question,
        external_knowledge=external_knowledge,
        schema_snapshot=schema_snapshot,
        entity_candidates=entity_candidates,
        relation_candidates=relation_candidates or [],
        include_relation_candidates=include_relation_candidates,
        prompt_dir=prompt_dir,
        prompt_template_name=prompt_template_name,
    )
    raw_response = llm.single_turn(
        prompt,
        check_func=json_check,
    )
    if not raw_response.strip():
        return {
            "ok": False,
            "error": "LLM returned empty response.",
            "prompt": prompt,
            "raw_response": raw_response,
            "selection": {"entities": [], "selected_entities": [], "relations": [], "selected_relations": []},
            "model_config": resolved_model_config_name,
        }

    try:
        parsed = json_parse(raw_response)
        selection = _sanitize_selection_payload(
            parsed,
            entity_candidates=entity_candidates,
            relation_candidates=relation_candidates or [],
            include_relation_candidates=include_relation_candidates,
        )
        return {
            "ok": True,
            "error": "",
            "prompt": prompt,
            "raw_response": raw_response,
            "selection": selection,
            "model_config": resolved_model_config_name,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "error": str(exc),
            "prompt": prompt,
            "raw_response": raw_response,
            "selection": {"entities": [], "selected_entities": [], "relations": [], "selected_relations": []},
            "model_config": resolved_model_config_name,
        }


__all__ = [
    "DEFAULT_PROMPT_DIR",
    "DEFAULT_PROMPT_TEMPLATE_NAME",
    "DEFAULT_SAMPLE_ROW_LIMIT",
    "DEFAULT_SAMPLE_VALUE_MAX_CHARS",
    "build_entity_candidates",
    "build_relation_candidates",
    "build_schema_snapshot",
    "merge_er_models",
    "select_entities_with_llm",
]
