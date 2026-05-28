from __future__ import annotations

from pathlib import Path
from typing import Any

from src.er2data.schema_utils import read_json_object, resolve_path, table_fullname_from_column
from src.er2data.table_grouping import TableGroup, normalize_selected_member_tables, resolve_group_column_fullnames
from src.nl2er.table_semantic_sketch import (
    collect_evidence_columns_from_unit,
    normalize_database_group_profile,
    profile_is_relevant_supportive,
    unique_nonempty_strings,
)


def copy_first_present(source: dict[str, Any], target: dict[str, Any], *keys: str) -> None:
    for key in keys:
        if key in source:
            target[key] = source[key]
            return


def ensure_dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def strip_entity_definitions(value: Any) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for entity in ensure_dict_list(value):
        definition: dict[str, Any] = {}
        copy_first_present(entity, definition, "entity_name", "name")
        copy_first_present(entity, definition, "desc", "description")
        copy_first_present(entity, definition, "grain")
        definitions.append(definition)
    return definitions


def strip_relation_participants(value: Any) -> list[dict[str, Any]]:
    participants: list[dict[str, Any]] = []
    for participant in ensure_dict_list(value):
        definition: dict[str, Any] = {}
        copy_first_present(participant, definition, "role")
        copy_first_present(participant, definition, "entity", "entity_name")
        if definition:
            participants.append(definition)
    return participants


def strip_relation_definitions(value: Any) -> list[dict[str, Any]]:
    definitions: list[dict[str, Any]] = []
    for relation in ensure_dict_list(value):
        definition: dict[str, Any] = {}
        copy_first_present(relation, definition, "relation_name", "name")
        copy_first_present(relation, definition, "desc", "description")
        copy_first_present(relation, definition, "grain")
        participants = strip_relation_participants(relation.get("participants"))
        if participants:
            definition["participants"] = participants
        definitions.append(definition)
    return definitions


def resolve_logical_model_path(
    *,
    case_dir: Path,
    logical_model_path: Path | None = None,
    nl2er_output_path: Path | None = None,
    logical_model_filename: str = "",
    nl2er_output_filename: str = "",
    default_filename: str = "nl2er_output.json",
) -> Path:
    explicit_path = logical_model_path or nl2er_output_path
    if explicit_path is not None:
        return resolve_path(explicit_path)
    filename = (
        str(logical_model_filename or "").strip()
        or str(nl2er_output_filename or "").strip()
        or default_filename
    )
    return (case_dir / filename).resolve()


def read_logical_model(
    *,
    case_dir: Path,
    logical_model_path: Path | None = None,
    nl2er_output_path: Path | None = None,
    logical_model_filename: str = "",
    nl2er_output_filename: str = "",
    default_filename: str = "nl2er_output.json",
) -> dict[str, Any]:
    path = resolve_logical_model_path(
        case_dir=case_dir,
        logical_model_path=logical_model_path,
        nl2er_output_path=nl2er_output_path,
        logical_model_filename=logical_model_filename,
        nl2er_output_filename=nl2er_output_filename,
        default_filename=default_filename,
    )
    if not path.exists():
        raise FileNotFoundError(f"Logical model file does not exist: {path}")
    payload = read_json_object(path)
    standard_keys = {"entities", "relations", "conditions", "operations", "resolve_process"}
    extra = {
        key: value
        for key, value in payload.items()
        if key not in standard_keys
    }
    return {
        "source_path": str(path),
        "entities": strip_entity_definitions(payload.get("entities")),
        "relations": strip_relation_definitions(payload.get("relations")),
        "conditions": payload.get("conditions") if isinstance(payload.get("conditions"), list) else [],
        "operations": payload.get("operations") if isinstance(payload.get("operations"), list) else [],
        "resolve_process": payload.get("resolve_process") if isinstance(payload.get("resolve_process"), list) else [],
        "extra": extra,
    }


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
        "table_groups": [
            serialize_context_table_group(group)
            for group in context_groups
        ],
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


def extract_er_review_response_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "table_er_semantic_review",
        "database_table_er_semantic_review",
        "er_semantic_review",
        "table_group_profile",
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
                "is_relevant_supportive",
                "is_relevant",
                "member_table_scope",
                "selected_member_tables",
                "linked_member_tables",
            ):
                if metadata_key in parsed and metadata_key not in merged:
                    merged[metadata_key] = parsed[metadata_key]
            return merged
    return parsed


def normalize_dict_list(value: Any) -> list[dict[str, Any]]:
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def collect_column_references(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return unique_nonempty_strings(value)
    if not isinstance(value, list):
        return []

    columns: list[str] = []
    for item in value:
        if isinstance(item, str):
            columns.append(item)
            continue
        if not isinstance(item, dict):
            continue
        columns.extend(
            unique_nonempty_strings(
                [
                    item.get("column_fullname"),
                    item.get("column_name"),
                    item.get("name"),
                    item.get("field_name"),
                ]
            )
        )
        columns.extend(unique_nonempty_strings(item.get("evidence_columns")))
    return unique_nonempty_strings(columns)


def er_review_has_semantic_support(
    *,
    supported_logical_entities: list[dict[str, Any]],
    supported_logical_relations: list[dict[str, Any]],
    derived_representations: list[dict[str, Any]],
    intermediate_representations: list[dict[str, Any]],
    derived_semantics: list[dict[str, Any]],
) -> bool:
    return any(
        (
            supported_logical_entities,
            supported_logical_relations,
            derived_representations,
            intermediate_representations,
            derived_semantics,
        )
    )


def normalize_er_review_profile(
    *,
    parsed: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    payload = extract_er_review_response_payload(parsed)
    supported_logical_entities = normalize_dict_list(payload.get("supported_logical_entities"))
    supported_logical_relations = normalize_dict_list(payload.get("supported_logical_relations"))
    derived_representations = normalize_dict_list(payload.get("derived_representations"))
    intermediate_representations = normalize_dict_list(payload.get("intermediate_representations"))
    derived_semantics = normalize_dict_list(payload.get("derived_semantics"))
    is_relevant = er_review_has_semantic_support(
        supported_logical_entities=supported_logical_entities,
        supported_logical_relations=supported_logical_relations,
        derived_representations=derived_representations,
        intermediate_representations=intermediate_representations,
        derived_semantics=derived_semantics,
    )

    profile_input = dict(payload)
    profile_input["is_relevant_supportive"] = is_relevant
    profile_input["is_relevant"] = is_relevant
    if is_relevant and str(profile_input.get("member_table_scope") or "").strip().casefold() == "none":
        profile_input["member_table_scope"] = "all_members"

    profile = normalize_database_group_profile(parsed=profile_input, item=item)
    profile["is_relevant_supportive"] = is_relevant
    profile["is_relevant"] = is_relevant
    profile["supported_logical_entities"] = supported_logical_entities
    profile["supported_logical_relations"] = supported_logical_relations
    profile["derived_representations"] = derived_representations
    profile["intermediate_representations"] = intermediate_representations
    profile["derived_semantics"] = derived_semantics
    profile["other_columns"] = collect_column_references(payload.get("other_columns"))
    profile["linked_columns"] = collect_relevant_columns_from_er_profile(profile)
    profile.pop("semantic_units", None)

    if not is_relevant:
        profile["member_table_scope"] = "none"
        profile["selected_member_tables"] = []
        profile["supported_question_semantics"] = []
        profile["supported_logical_entities"] = []
        profile["supported_logical_relations"] = []
        profile["derived_representations"] = []
        profile["intermediate_representations"] = []
        profile["derived_semantics"] = []
        profile["other_columns"] = []
        profile["linked_columns"] = []
        profile.pop("semantic_units", None)
    return profile


def collect_relevant_columns_from_er_profile(profile: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    columns.extend(unique_nonempty_strings(profile.get("linked_columns")))
    columns.extend(unique_nonempty_strings(profile.get("other_columns")))
    for key in (
        "supported_logical_entities",
        "supported_logical_relations",
        "derived_representations",
        "intermediate_representations",
        "derived_semantics",
    ):
        for item in normalize_dict_list(profile.get(key)):
            columns.extend(unique_nonempty_strings(item.get("evidence_columns")))
            for participant in normalize_dict_list(item.get("supported_participants")):
                columns.extend(unique_nonempty_strings(participant.get("evidence_columns")))
    for unit in normalize_dict_list(profile.get("semantic_units")):
        columns.extend(collect_evidence_columns_from_unit(unit))
    return unique_nonempty_strings(columns)


def collect_er_schema_selection(
    *,
    group_payloads: list[dict[str, Any]],
    group_profiles: list[dict[str, Any]],
) -> tuple[list[str], list[str]]:
    linked_tables: list[str] = []
    linked_columns: list[str] = []
    seen_tables: set[str] = set()
    seen_columns: set[str] = set()

    def add_table(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_tables:
            return
        linked_tables.append(text)
        seen_tables.add(text)

    def add_column(value: Any) -> None:
        text = str(value or "").strip()
        if not text or text in seen_columns:
            return
        linked_columns.append(text)
        seen_columns.add(text)
        table_fullname = table_fullname_from_column(text)
        if table_fullname:
            add_table(table_fullname)

    for group_payload, profile in zip(group_payloads, group_profiles):
        if not profile_is_relevant_supportive(profile):
            continue
        selected_member_tables = normalize_selected_member_tables(profile, group_payload)
        for table_fullname in selected_member_tables:
            add_table(table_fullname)
        for column_fullname in resolve_group_column_fullnames(
            collect_relevant_columns_from_er_profile(profile),
            group_payload=group_payload,
            selected_member_tables=selected_member_tables,
        ):
            add_column(column_fullname)
    return linked_tables, linked_columns
