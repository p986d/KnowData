from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from src.er2data.physical_schema import TableMetadata, table_metadata_to_prompt_snapshot
from src.er2data.schema_utils import (
    column_name_from_fullname,
    safe_file_stem,
    table_fullname_from_column,
)


GROUPING_METHOD = "reforce_table_family"


@dataclass(frozen=True, slots=True)
class TableGroup:
    group_id: str
    representative: TableMetadata
    members: list[TableMetadata]
    group_key: dict[str, Any]


def clear_tb(text: str) -> str:
    return str(text or "").replace('"', "").replace("`", "").strip().upper()


def remove_digits(text: str) -> str:
    return re.sub(r"\d", "", str(text or ""))


def normalize_casefold_set(values: list[str] | None) -> set[str]:
    filters: set[str] = set()
    for value in values or []:
        for item in str(value or "").split(","):
            text = item.strip()
            if text:
                filters.add(text.casefold())
    return filters


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


def sanitize_table_snapshot(
    table: dict[str, Any],
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> dict[str, Any]:
    sanitized = dict(table)
    sanitized["sample_rows"] = [
        {
            key: str(value)[:sample_value_max_chars]
            if value is not None and not isinstance(value, (int, float, bool))
            else value
            for key, value in row.items()
        }
        for row in sanitized.get("sample_rows", [])[:sample_row_limit]
        if isinstance(row, dict)
    ]
    columns: list[dict[str, Any]] = []
    for column in sanitized.get("columns", []):
        if not isinstance(column, dict):
            continue
        item = dict(column)
        item["sample_values"] = [
            str(value)[:sample_value_max_chars]
            if value is not None and not isinstance(value, (int, float, bool))
            else value
            for value in item.get("sample_values", [])[:sample_values_per_column]
        ]
        columns.append(item)
    sanitized["columns"] = columns
    return sanitized


def normalized_column_signature(
    column_names: list[str],
    column_types: list[str],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            (
                (clear_tb(column_name), clear_tb(column_type))
                for column_name, column_type in zip(column_names, column_types)
            ),
            key=lambda item: (item[0], item[1]),
        )
    )


def column_signature_hash(column_names: list[str], column_types: list[str]) -> str:
    payload = json.dumps(
        normalized_column_signature(column_names, column_types),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def build_group_key(table: TableMetadata) -> tuple[str, str, tuple[tuple[str, str], ...]]:
    columns_signature = normalized_column_signature(
        table.column_names,
        table.column_types,
    )
    return clear_tb(table.namespace), remove_digits(clear_tb(table.short_name)), columns_signature


def build_table_groups(tables: list[TableMetadata]) -> list[TableGroup]:
    grouped: dict[tuple[str, str, tuple[tuple[str, str], ...]], list[TableMetadata]] = defaultdict(list)
    for table in tables:
        grouped[build_group_key(table)].append(table)

    results: list[TableGroup] = []
    for index, (key, members) in enumerate(sorted(grouped.items(), key=lambda item: item[1][0].full_name)):
        members = sorted(members, key=lambda item: item.full_name)
        representative = members[0]
        namespace, normalized_short_name, _ = key
        signature_hash = column_signature_hash(
            representative.column_names,
            representative.column_types,
        )
        group_id = f"group_{index + 1:04d}_{safe_file_stem(representative.full_name)}"
        results.append(
            TableGroup(
                group_id=group_id,
                representative=representative,
                members=members,
                group_key={
                    "namespace": namespace,
                    "normalized_table_name": normalized_short_name,
                    "column_signature_hash": signature_hash,
                },
            )
        )
    return results


def serialize_table_member(table: TableMetadata) -> dict[str, str]:
    return {
        "table_fullname": table.full_name,
        "namespace": table.namespace,
        "table_name": table.short_name,
        "snapshot_path": table.snapshot_path,
    }


def build_group_prompt_snapshot(
    group: TableGroup,
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> dict[str, Any]:
    representative_snapshot = table_metadata_to_prompt_snapshot(
        group.representative,
        sample_values_per_column=sample_values_per_column,
    )
    representative_snapshot = sanitize_table_snapshot(
        representative_snapshot,
        sample_row_limit=sample_row_limit,
        sample_values_per_column=sample_values_per_column,
        sample_value_max_chars=sample_value_max_chars,
    )
    representative_snapshot["table_group"] = {
        "group_id": group.group_id,
        "grouping_method": GROUPING_METHOD,
        "group_key": group.group_key,
        "representative_table": group.representative.full_name,
        "family_size": len(group.members),
        "member_tables": [serialize_table_member(member) for member in group.members],
    }
    return representative_snapshot


def serialize_group(group: TableGroup) -> dict[str, Any]:
    return {
        "group_id": group.group_id,
        "grouping_method": GROUPING_METHOD,
        "group_key": group.group_key,
        "representative_table": group.representative.full_name,
        "member_tables": [member.full_name for member in group.members],
        "family_size": len(group.members),
    }


def group_payloads_from_groups(
    groups: list[TableGroup],
    *,
    sample_row_limit: int,
    sample_values_per_column: int,
    sample_value_max_chars: int,
) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for group in groups:
        payloads.append(
            {
                "group_id": group.group_id,
                "table_fullname": group.representative.full_name,
                "target_table": build_group_prompt_snapshot(
                    group,
                    sample_row_limit=sample_row_limit,
                    sample_values_per_column=sample_values_per_column,
                    sample_value_max_chars=sample_value_max_chars,
                ),
                "linked_columns": [],
                "group": serialize_group(group),
            }
        )
    return payloads


def filter_groups_by_table_fullname(
    groups: list[TableGroup],
    *,
    table_filters: set[str],
) -> list[TableGroup]:
    if not table_filters:
        return groups
    selected: list[TableGroup] = []
    for group in groups:
        member_names = {member.full_name.casefold() for member in group.members}
        if member_names & table_filters:
            selected.append(group)
    return selected


def normalize_selected_member_tables(profile: dict[str, Any], group_payload: dict[str, Any]) -> list[str]:
    group = group_payload.get("group") if isinstance(group_payload.get("group"), dict) else {}
    member_tables = unique_nonempty_strings(group.get("member_tables"))
    member_lookup = {table.casefold(): table for table in member_tables}
    selected = unique_nonempty_strings(
        profile.get("selected_member_tables")
        or profile.get("linked_member_tables")
        or profile.get("member_tables")
    )
    resolved = [
        member_lookup[item.casefold()]
        for item in selected
        if item.casefold() in member_lookup
    ]
    return resolved or member_tables


def resolve_group_column_fullnames(
    values: list[str],
    *,
    group_payload: dict[str, Any],
    selected_member_tables: list[str],
) -> list[str]:
    target_table = group_payload.get("target_table")
    if not isinstance(target_table, dict):
        return []
    columns = [item for item in target_table.get("columns") or [] if isinstance(item, dict)]
    available_by_name: dict[str, str] = {}
    available_fullnames: dict[str, str] = {}
    for column in columns:
        column_name = str(column.get("column_name") or "").strip()
        column_fullname = str(column.get("column_fullname") or "").strip()
        if column_name:
            available_by_name[column_name.casefold()] = column_name
        if column_fullname:
            available_fullnames[column_fullname.casefold()] = column_fullname

    member_lookup = {table.casefold(): table for table in selected_member_tables}
    resolved: list[str] = []
    seen: set[str] = set()

    def add_column(table_fullname: str, column_name: str) -> None:
        if not table_fullname or not column_name:
            return
        value = f"{table_fullname}.{column_name}"
        if value in seen:
            return
        resolved.append(value)
        seen.add(value)

    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        source_table = table_fullname_from_column(text)
        if source_table:
            column_name = column_name_from_fullname(text)
            known_column_name = available_by_name.get(column_name.casefold(), column_name)
            if text.casefold() in available_fullnames:
                for member_table in selected_member_tables:
                    add_column(member_table, known_column_name)
                continue
            if source_table.casefold() in member_lookup:
                add_column(member_lookup[source_table.casefold()], known_column_name)
                continue
            continue

        column_name = available_by_name.get(text.casefold())
        if not column_name:
            continue
        for member_table in selected_member_tables:
            add_column(member_table, column_name)

    return resolved
