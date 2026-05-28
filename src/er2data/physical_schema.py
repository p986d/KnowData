from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.er2data.schema_utils import (
    read_json_object,
    resolve_snapshot_search_roots,
    sample_column_values,
)


@dataclass(frozen=True, slots=True)
class TableMetadata:
    full_name: str
    namespace: str
    short_name: str
    column_names: list[str]
    column_types: list[str]
    descriptions: list[str]
    sample_rows: list[dict[str, Any]]
    snapshot_path: str
    table_description: str = ""
    explicit_constraints: dict[str, Any] | None = None


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if not isinstance(value, list):
        return []
    output: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        output.append(text)
        seen.add(text)
    return output


def _normalize_column_group(value: Any) -> list[str]:
    if isinstance(value, dict):
        return _string_list(
            value.get("columns")
            or value.get("column_names")
            or value.get("key_columns")
        )
    return _string_list(value)


def _normalize_column_groups(value: Any) -> list[list[str]]:
    if value is None:
        return []
    if isinstance(value, (str, dict)):
        group = _normalize_column_group(value)
        return [group] if group else []
    if not isinstance(value, list):
        return []
    groups: list[list[str]] = []
    for item in value:
        group = _normalize_column_group(item)
        if group:
            groups.append(group)
    return groups


def _normalize_foreign_keys(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict)]
    else:
        candidates = []

    output: list[dict[str, Any]] = []
    for item in candidates:
        columns = _string_list(
            item.get("columns")
            or item.get("source_columns")
            or item.get("foreign_key_columns")
            or item.get("column_names")
        )
        referenced_columns = _string_list(
            item.get("referenced_columns")
            or item.get("target_columns")
            or item.get("primary_key_columns")
        )
        referenced_table = str(
            item.get("referenced_table")
            or item.get("target_table")
            or item.get("references_table")
            or ""
        ).strip()
        if not columns and not referenced_table and not referenced_columns:
            continue
        output.append(
            {
                "name": str(item.get("name") or item.get("constraint_name") or "").strip(),
                "columns": columns,
                "referenced_table": referenced_table,
                "referenced_columns": referenced_columns,
            }
        )
    return output


def _normalize_indexes(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [item for item in value if isinstance(item, dict)]
    else:
        candidates = []

    output: list[dict[str, Any]] = []
    for item in candidates:
        columns = _string_list(item.get("columns") or item.get("column_names"))
        if not columns:
            continue
        output.append(
            {
                "name": str(item.get("name") or item.get("index_name") or "").strip(),
                "columns": columns,
                "is_unique": bool(item.get("is_unique") or item.get("unique")),
            }
        )
    return output


def normalize_explicit_constraints(payload: dict[str, Any]) -> dict[str, Any]:
    primary_keys = _normalize_column_group(
        payload.get("primary_keys")
        or payload.get("primary_key")
        or payload.get("primary_key_columns")
        or payload.get("pk")
    )
    unique_keys = _normalize_column_groups(
        payload.get("unique_keys")
        or payload.get("unique_constraints")
        or payload.get("unique_key_columns")
    )
    foreign_keys = _normalize_foreign_keys(
        payload.get("foreign_keys")
        or payload.get("foreign_key")
        or payload.get("fk_constraints")
    )
    indexes = _normalize_indexes(payload.get("indexes") or payload.get("indices"))
    return {
        "primary_keys": primary_keys,
        "unique_keys": unique_keys,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
    }


def load_database_tables(
    *,
    db_id: str,
    database_root: str | Path | None,
    spider2_root: str | Path | None,
) -> list[TableMetadata]:
    search_roots = resolve_snapshot_search_roots(
        db_id=db_id,
        database_root=database_root,
        spider2_root=spider2_root,
    )
    if not search_roots:
        raise FileNotFoundError(
            f"No database schema roots were found for db_id `{db_id}`."
        )

    tables_by_fullname: dict[str, TableMetadata] = {}
    for root in search_roots:
        for json_path in sorted(root.rglob("*.json")):
            try:
                payload = read_json_object(json_path)
            except Exception:
                continue
            full_name = str(payload.get("table_fullname") or "").strip()
            column_names = payload.get("column_names")
            if not full_name or not isinstance(column_names, list) or not column_names:
                continue
            parts = [part.strip() for part in full_name.split(".") if part.strip()]
            if len(parts) < 2:
                continue

            column_types = payload.get("column_types") or []
            descriptions = payload.get("description") or []
            sample_rows = payload.get("sample_rows") or []
            if not isinstance(column_types, list):
                column_types = []
            if not isinstance(descriptions, list):
                descriptions = []
            if not isinstance(sample_rows, list):
                sample_rows = []

            if len(column_types) < len(column_names):
                column_types = list(column_types) + [""] * (len(column_names) - len(column_types))
            if len(descriptions) < len(column_names):
                descriptions = list(descriptions) + [""] * (len(column_names) - len(descriptions))

            key = full_name.casefold()
            if key in tables_by_fullname:
                continue
            tables_by_fullname[key] = TableMetadata(
                full_name=full_name,
                namespace=".".join(parts[:-1]),
                short_name=parts[-1],
                column_names=[str(name) for name in column_names],
                column_types=[str(value or "") for value in column_types[: len(column_names)]],
                descriptions=[str(value or "") for value in descriptions[: len(column_names)]],
                sample_rows=[row for row in sample_rows if isinstance(row, dict)],
                snapshot_path=str(json_path),
                table_description=str(
                    payload.get("table_description")
                    or payload.get("description_text")
                    or ""
                ).strip(),
                explicit_constraints=normalize_explicit_constraints(payload),
            )

    tables = sorted(tables_by_fullname.values(), key=lambda item: item.full_name)
    if not tables:
        raise ValueError(f"No usable table JSON files found for db_id `{db_id}`.")
    return tables


def table_metadata_to_prompt_snapshot(
    table: TableMetadata,
    *,
    sample_values_per_column: int,
) -> dict[str, Any]:
    columns: list[dict[str, Any]] = []
    for column_name, column_type, description in zip(
        table.column_names,
        table.column_types,
        table.descriptions,
    ):
        columns.append(
            {
                "column_fullname": f"{table.full_name}.{column_name}",
                "column_name": column_name,
                "data_type": column_type,
                "description": description,
                "sample_values": sample_column_values(
                    table.sample_rows,
                    column_name,
                    limit=sample_values_per_column,
                ),
            }
        )

    return {
        "table_fullname": table.full_name,
        "table_name": table.short_name,
        "description": table.table_description,
        "snapshot_path": table.snapshot_path,
        "explicit_constraints": table.explicit_constraints or normalize_explicit_constraints({}),
        "columns": columns,
        "sample_rows": table.sample_rows[:sample_values_per_column],
    }
