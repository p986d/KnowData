from __future__ import annotations

import json
from typing import Any

from src.er2data.table_grouping import (
    GROUPING_METHOD,
    normalize_selected_member_tables,
    resolve_group_column_fullnames,
)
from src.er2data.schema_utils import ensure_dict_list, table_fullname_from_column


def unique_nonempty_strings(values: Any) -> list[str]:
    if values is None:
        return []
    if isinstance(values, str):
        candidates = [values]
    elif isinstance(values, list):
        candidates = values
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


def collect_evidence_columns_from_unit(unit: dict[str, Any]) -> list[str]:
    columns: list[str] = []
    columns.extend(unique_nonempty_strings(unit.get("evidence_columns")))
    for attribute in ensure_dict_list(unit.get("attributes")):
        columns.extend(unique_nonempty_strings(attribute.get("evidence_columns")))
    for participant in ensure_dict_list(unit.get("participants")):
        columns.extend(unique_nonempty_strings(participant.get("evidence_columns")))
    return unique_nonempty_strings(columns)


def normalize_model_profile(
    *,
    parsed: dict[str, Any],
    table_fullname: str,
    target_table: dict[str, Any],
) -> dict[str, Any]:
    del target_table
    payload = parsed
    raw_units = payload.get("semantic_units")
    if raw_units is None:
        raw_units = payload.get("units")
    semantic_units: list[dict[str, Any]] = []
    for index, unit in enumerate(ensure_dict_list(raw_units)):
        unit_name = str(
            unit.get("unit_name")
            or unit.get("name")
            or unit.get("entity_name")
            or unit.get("relation_name")
            or f"unit_{index + 1}"
        ).strip()
        attributes = []
        raw_attributes = unit.get("attributes")
        if raw_attributes is None:
            raw_attributes = unit.get("related_attributes")
        if isinstance(raw_attributes, list):
            for attribute in raw_attributes:
                if isinstance(attribute, dict):
                    attributes.append(
                        {
                            "name": str(attribute.get("name") or attribute.get("attribute_name") or "").strip(),
                            "semantics": str(attribute.get("semantics") or attribute.get("description") or "").strip(),
                            "evidence_columns": unique_nonempty_strings(attribute.get("evidence_columns")),
                        }
                    )
                else:
                    attributes.append(
                        {
                            "name": str(attribute or "").strip(),
                            "semantics": "",
                            "evidence_columns": unique_nonempty_strings(unit.get("evidence_columns")),
                        }
                    )
        semantic_units.append(
            {
                "unit_type": str(unit.get("unit_type") or "entity").strip(),
                "unit_name": unit_name,
                "desc": str(unit.get("desc") or unit.get("description") or "").strip(),
                "grain": str(unit.get("grain") or "").strip(),
                "attributes": [item for item in attributes if item["name"]],
                "participants": ensure_dict_list(unit.get("participants")),
            }
        )
    supported_semantics = unique_nonempty_strings(
        payload.get("supported_question_semantics")
        or payload.get("semantics")
        or payload.get("description")
    )
    relevance = normalize_bool(payload.get("is_relevant_supportive"))
    if relevance is None:
        relevance = normalize_bool(payload.get("is_relevant"))
    if relevance is None:
        relevance = bool(semantic_units or supported_semantics)
    return {
        "table_fullname": table_fullname,
        "supported_question_semantics": supported_semantics,
        "is_relevant_supportive": relevance,
        "is_relevant": relevance,
        "semantic_units": semantic_units,
    }


def collect_group_schema_selection(
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

        evidence_columns: list[str] = []
        evidence_columns.extend(unique_nonempty_strings(profile.get("linked_columns")))
        evidence_columns.extend(unique_nonempty_strings(profile.get("other_columns")))
        for unit in ensure_dict_list(profile.get("semantic_units")):
            evidence_columns.extend(collect_evidence_columns_from_unit(unit))
        for column_fullname in resolve_group_column_fullnames(
            unique_nonempty_strings(evidence_columns),
            group_payload=group_payload,
            selected_member_tables=selected_member_tables,
        ):
            add_column(column_fullname)

    return linked_tables, linked_columns


def profile_is_relevant_supportive(profile: dict[str, Any]) -> bool:
    value = profile.get("is_relevant_supportive")
    if isinstance(value, bool):
        return value
    return bool(profile.get("is_relevant"))


def build_compact_semantic_output(group_profiles: list[dict[str, Any]]) -> dict[str, Any]:
    semantics_units: list[dict[str, Any]] = []
    relevant_table_entities: list[dict[str, Any]] = []
    table_other_columns: list[dict[str, Any]] = []
    seen_unit_signatures: set[str] = set()

    for profile in group_profiles:
        if not profile_is_relevant_supportive(profile):
            continue
        representative_table = str(profile.get("representative_table") or "").strip()
        profile_units = ensure_dict_list(profile.get("semantic_units"))
        other_columns = unique_nonempty_strings(profile.get("other_columns"))
        entity_units = [
            unit
            for unit in profile_units
            if str(unit.get("unit_type") or "").strip().casefold() == "entity"
        ]
        relevant_table_entities.append(
            {
                "table_fullname": representative_table,
                "entity_definitions": entity_units,
                "other_columns": other_columns,
            }
        )
        if other_columns:
            table_other_columns.append(
                {
                    "table_fullname": representative_table,
                    "other_columns": other_columns,
                }
            )
        for unit in profile_units:
            compact_unit = {
                "unit_name": str(unit.get("unit_name") or "").strip(),
                "unit_type": str(unit.get("unit_type") or "").strip(),
                "desc": str(unit.get("desc") or "").strip(),
                "grain": str(unit.get("grain") or "").strip(),
            }
            if compact_unit["unit_type"].casefold() == "relationship":
                compact_unit["participants"] = ensure_dict_list(unit.get("participants"))
            try:
                signature = json.dumps(compact_unit, ensure_ascii=False, sort_keys=True)
            except Exception:
                signature = str(compact_unit)
            if signature in seen_unit_signatures:
                continue
            semantics_units.append(compact_unit)
            seen_unit_signatures.add(signature)

    return {
        "semantics_units": semantics_units,
        "relevant_table_entities": relevant_table_entities,
        "table_other_columns": table_other_columns,
    }


def first_nonempty_string(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def normalize_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().casefold()
    if not text:
        return None
    if text in {"true", "yes", "y", "1", "relevant", "linked", "相关"}:
        return True
    if text in {"false", "no", "n", "0", "irrelevant", "unlinked", "不相关"}:
        return False
    return None


def nested_dict(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    return value if isinstance(value, dict) else {}


def extract_group_response_payload(parsed: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "table_group_profile",
        "group_profile",
        "database_table_semantic_sketch",
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
                "other_columns",
            ):
                if metadata_key in parsed and metadata_key not in merged:
                    merged[metadata_key] = parsed[metadata_key]
            return merged
    return parsed


def collect_response_strings(payload: dict[str, Any], *keys: str) -> list[str]:
    values: list[str] = []
    for key in keys:
        values.extend(unique_nonempty_strings(payload.get(key)))
    return unique_nonempty_strings(values)


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


def normalize_database_group_profile(
    *,
    parsed: dict[str, Any],
    item: dict[str, Any],
) -> dict[str, Any]:
    payload = extract_group_response_payload(parsed)
    group = item.get("group") if isinstance(item.get("group"), dict) else {}
    representative_table = str(
        group.get("representative_table") or item.get("table_fullname") or ""
    ).strip()

    schema_linking_payload = nested_dict(payload, "schema_linking")
    table_selection_payload = nested_dict(payload, "table_selection")
    table_group_payload = nested_dict(payload, "table_group")

    profile_input = dict(payload)
    raw_relevance = payload.get("is_relevant_supportive")
    if raw_relevance is None:
        raw_relevance = payload.get("is_relevant")
    normalized_relevance = normalize_bool(raw_relevance)
    if normalized_relevance is not None:
        profile_input["is_relevant_supportive"] = normalized_relevance
        profile_input["is_relevant"] = normalized_relevance

    profile = normalize_model_profile(
        parsed=profile_input,
        table_fullname=representative_table,
        target_table=item["target_table"],
    )

    member_table_scope = first_nonempty_string(
        payload.get("member_table_scope"),
        payload.get("table_scope"),
        table_selection_payload.get("member_table_scope"),
        table_selection_payload.get("table_scope"),
        "all_members",
    )
    selected_member_tables = collect_response_strings(
        payload,
        "selected_member_tables",
        "linked_member_tables",
    )
    selected_member_tables.extend(
        collect_response_strings(
            table_selection_payload,
            "selected_member_tables",
            "linked_member_tables",
        )
    )
    linked_columns = collect_response_strings(payload, "linked_columns", "evidence_columns")
    linked_columns.extend(
        collect_response_strings(
            schema_linking_payload,
            "linked_columns",
            "evidence_columns",
            "columns",
        )
    )
    other_columns = collect_column_references(payload.get("other_columns"))

    if member_table_scope.casefold() == "none" or not bool(profile.get("is_relevant_supportive")):
        member_table_scope = "none"
        profile["is_relevant_supportive"] = False
        profile["is_relevant"] = False
        selected_member_tables = []
        linked_columns = []
        other_columns = []
        profile["semantic_units"] = []

    profile.update(
        {
            "group_id": first_nonempty_string(
                payload.get("group_id"),
                table_group_payload.get("group_id"),
                group.get("group_id"),
                item.get("group_id"),
            ),
            "grouping_method": GROUPING_METHOD,
            "representative_table": first_nonempty_string(
                payload.get("representative_table"),
                table_group_payload.get("representative_table"),
                representative_table,
            ),
            "member_tables": unique_nonempty_strings(group.get("member_tables")),
            "family_size": int(group.get("family_size") or 0),
            "member_table_scope": member_table_scope,
            "selected_member_tables": unique_nonempty_strings(selected_member_tables),
            "linked_columns": unique_nonempty_strings(linked_columns),
            "other_columns": unique_nonempty_strings(other_columns),
            "prompt_path": item.get("prompt_path"),
            "response_path": item.get("response_path"),
        }
    )
    return profile
