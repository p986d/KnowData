from __future__ import annotations

import json
from typing import Any

from src.er2data.schema_utils import ensure_dict_list
from src.er2data.table_grouping import GROUPING_METHOD, TableGroup
from src.nl2er.table_semantic_sketch import first_nonempty_string, unique_nonempty_strings


OUTPUT_SOURCE = "database_table_concept_extraction"


def serialize_context_table_group(group: TableGroup) -> dict[str, Any]:
    return {
        "representative_table": group.representative.full_name,
        "member_tables": [member.full_name for member in group.members],
    }


def build_table_group_context(
    *,
    target_group: TableGroup,
    table_groups: list[TableGroup],
    scope: str,
) -> dict[str, Any]:
    normalized_scope = str(scope or "schema").strip().casefold()
    if normalized_scope == "none":
        context_groups: list[TableGroup] = []
    elif normalized_scope == "database":
        context_groups = table_groups
    else:
        target_namespace = target_group.representative.namespace.casefold()
        context_groups = [
            group
            for group in table_groups
            if group.representative.namespace.casefold() == target_namespace
        ]
        normalized_scope = "schema"

    return {
        "scope": normalized_scope,
        "target_group_id": target_group.group_id,
        "target_namespace": target_group.representative.namespace,
        "table_group_count": len(context_groups),
        "table_groups": [serialize_context_table_group(group) for group in context_groups],
    }


def attach_table_group_contexts(
    *,
    table_groups: list[TableGroup],
    group_payloads: list[dict[str, Any]],
    scope: str,
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for group, payload in zip(table_groups, group_payloads):
        target_table = dict(payload.get("target_table") or {})
        target_table["table_group_context"] = build_table_group_context(
            target_group=group,
            table_groups=table_groups,
            scope=scope,
        )
        output.append({**payload, "target_table": target_table})
    return output


def extract_concept_response_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "database_table_concept_extraction",
        "table_concept_profile",
        "concept_extraction",
        "concept_profile",
        "group_profile",
        "profile",
        "result",
    ):
        value = parsed.get(key)
        if isinstance(value, dict):
            merged = dict(value)
            for metadata_key in (
                "group_id",
                "representative_table",
                "member_table_scope",
                "selected_member_tables",
            ):
                if metadata_key in parsed and metadata_key not in merged:
                    merged[metadata_key] = parsed[metadata_key]
            return merged
    return parsed


def normalize_column_object_list(value: Any) -> list[dict[str, Any]]:
    columns: list[dict[str, Any]] = []
    if not isinstance(value, list):
        return columns
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            if text:
                columns.append({"column": text})
            continue
        if not isinstance(item, dict):
            continue
        column_name = first_nonempty_string(
            item.get("column"),
            item.get("column_fullname"),
            item.get("column_name"),
            item.get("name"),
            item.get("field_name"),
        )
        if not column_name:
            continue
        normalized = dict(item)
        normalized["column"] = column_name
        columns.append(normalized)
    return columns


def collect_columns_from_concept(concept: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    columns.extend(unique_nonempty_strings(concept.get("evidence_columns")))
    columns.extend(unique_nonempty_strings(concept.get("identifier_columns")))
    for column in normalize_column_object_list(concept.get("attribute_columns")):
        columns.extend(unique_nonempty_strings([column.get("column")]))
        columns.extend(unique_nonempty_strings(column.get("evidence_columns")))
    for participant in ensure_dict_list(concept.get("participant_entities")):
        columns.extend(unique_nonempty_strings(participant.get("evidence_columns")))
    return unique_nonempty_strings(columns)


def normalize_concepts(value: Any) -> list[dict[str, Any]]:
    concepts: list[dict[str, Any]] = []
    for index, concept in enumerate(ensure_dict_list(value)):
        concept_name = first_nonempty_string(
            concept.get("concept_name"),
            concept.get("name"),
            concept.get("entity_name"),
            concept.get("relation_name"),
            f"concept_{index + 1}",
        )
        concept_type = first_nonempty_string(concept.get("concept_type"), concept.get("type"), "entity")
        normalized = {
            "concept_name": concept_name,
            "concept_type": concept_type,
            "description": first_nonempty_string(concept.get("description"), concept.get("desc")),
            "grain": str(concept.get("grain") or "").strip(),
            "identifier_columns": unique_nonempty_strings(
                concept.get("identifier_columns")
                or concept.get("primary_key_columns")
                or concept.get("key_columns")
            ),
            "attribute_columns": normalize_column_object_list(
                concept.get("attribute_columns")
                or concept.get("attributes")
                or concept.get("properties")
            ),
            "participant_entities": ensure_dict_list(
                concept.get("participant_entities")
                or concept.get("participants")
            ),
            "evidence_columns": unique_nonempty_strings(concept.get("evidence_columns")),
        }
        if not normalized["evidence_columns"]:
            normalized["evidence_columns"] = collect_columns_from_concept(normalized)
        concepts.append(normalized)
    return concepts


def normalize_foreign_key_candidates(value: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for item in ensure_dict_list(value):
        columns = unique_nonempty_strings(
            item.get("columns")
            or item.get("foreign_key_columns")
            or item.get("evidence_columns")
        )
        if not columns:
            continue
        candidates.append(
            {
                "columns": columns,
                "referenced_concept_or_table": first_nonempty_string(
                    item.get("referenced_concept_or_table"),
                    item.get("referenced_concept"),
                    item.get("referenced_table"),
                    item.get("target"),
                ),
                "relationship_semantics": first_nonempty_string(
                    item.get("relationship_semantics"),
                    item.get("semantics"),
                    item.get("description"),
                    item.get("reason"),
                ),
                "confidence": str(item.get("confidence") or "").strip(),
            }
        )
    return candidates


def collect_profile_columns(profile: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    columns.extend(unique_nonempty_strings(profile.get("evidence_columns")))
    columns.extend(unique_nonempty_strings(profile.get("metadata_columns")))
    for concept in ensure_dict_list(profile.get("concepts")):
        columns.extend(collect_columns_from_concept(concept))
    for candidate in ensure_dict_list(profile.get("foreign_key_candidates")):
        columns.extend(unique_nonempty_strings(candidate.get("columns")))
    return unique_nonempty_strings(columns)


def normalize_concept_profile(
    *,
    parsed: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    payload = extract_concept_response_payload(parsed)
    group = item.get("group") if isinstance(item.get("group"), dict) else {}
    representative_table = first_nonempty_string(
        payload.get("representative_table"),
        group.get("representative_table"),
        item.get("table_fullname"),
    )
    member_tables = unique_nonempty_strings(group.get("member_tables")) or [representative_table]
    concepts = normalize_concepts(
        payload.get("concepts")
        or payload.get("conceptual_units")
        or payload.get("semantic_units")
    )
    foreign_key_candidates = normalize_foreign_key_candidates(
        payload.get("foreign_key_candidates")
        or payload.get("relationships")
    )
    metadata_columns = unique_nonempty_strings(
        payload.get("metadata_columns")
        or payload.get("system_columns")
        or payload.get("other_columns")
    )
    profile = {
        "group_id": first_nonempty_string(payload.get("group_id"), group.get("group_id"), item.get("group_id")),
        "grouping_method": GROUPING_METHOD,
        "representative_table": representative_table,
        "member_tables": member_tables,
        "family_size": int(group.get("family_size") or len(member_tables)),
        "member_table_scope": first_nonempty_string(payload.get("member_table_scope"), "all_members"),
        "selected_member_tables": unique_nonempty_strings(payload.get("selected_member_tables")),
        "concepts": concepts,
        "foreign_key_candidates": foreign_key_candidates,
        "metadata_columns": metadata_columns,
        "quality_warnings": unique_nonempty_strings(payload.get("quality_warnings") or payload.get("warnings")),
        "prompt_path": item.get("prompt_path"),
        "response_path": item.get("response_path"),
    }
    profile["evidence_columns"] = collect_profile_columns(profile)
    return profile


def build_compact_concept_output(group_profiles: list[dict[str, Any]]) -> dict[str, Any]:
    concepts: list[dict[str, Any]] = []
    foreign_key_candidates: list[dict[str, Any]] = []
    metadata_columns_by_table: list[dict[str, Any]] = []
    seen_concepts: set[str] = set()
    seen_fk: set[str] = set()

    for profile in group_profiles:
        representative_table = str(profile.get("representative_table") or "").strip()
        for concept in ensure_dict_list(profile.get("concepts")):
            compact = dict(concept)
            compact["source_table_group"] = profile.get("group_id", "")
            compact["representative_table"] = representative_table
            try:
                signature = json.dumps(
                    {
                        "concept_name": compact.get("concept_name", ""),
                        "concept_type": compact.get("concept_type", ""),
                        "grain": compact.get("grain", ""),
                        "representative_table": representative_table,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )
            except Exception:
                signature = str(compact)
            if signature in seen_concepts:
                continue
            concepts.append(compact)
            seen_concepts.add(signature)

        for candidate in ensure_dict_list(profile.get("foreign_key_candidates")):
            compact_fk = dict(candidate)
            compact_fk["source_table_group"] = profile.get("group_id", "")
            compact_fk["representative_table"] = representative_table
            signature = json.dumps(compact_fk, ensure_ascii=False, sort_keys=True, default=str)
            if signature in seen_fk:
                continue
            foreign_key_candidates.append(compact_fk)
            seen_fk.add(signature)

        metadata_columns = unique_nonempty_strings(profile.get("metadata_columns"))
        if metadata_columns:
            metadata_columns_by_table.append(
                {
                    "representative_table": representative_table,
                    "member_tables": unique_nonempty_strings(profile.get("member_tables")),
                    "metadata_columns": metadata_columns,
                }
            )

    return {
        "concepts": concepts,
        "foreign_key_candidates": foreign_key_candidates,
        "metadata_columns_by_table": metadata_columns_by_table,
    }
