from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.er2data.physical_schema import TableMetadata, load_database_tables, normalize_explicit_constraints
from src.er2data.schema_utils import DEFAULT_SAMPLE_VALUES_PER_COLUMN, safe_file_stem, sample_column_values
from src.er2data.table_grouping import GROUPING_METHOD, TableGroup, build_table_groups
from src.dataconcept.models import (
    ColumnSampleStats,
    ColumnSnapshotEvidence,
    DataSnapshotEvidence,
    DataSnapshotRelationClue,
)


def _json_key(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _split_table_fullname(value: str) -> tuple[str, str]:
    parts = [part.strip() for part in str(value or "").split(".") if part.strip()]
    if len(parts) <= 1:
        return "", parts[0] if parts else ""
    return ".".join(parts[:-1]), parts[-1]


def build_column_sample_stats(
    *,
    sample_rows: list[dict[str, Any]],
    column_name: str,
) -> ColumnSampleStats:
    observed_values = [
        row.get(column_name)
        for row in sample_rows
        if isinstance(row, dict) and column_name in row
    ]
    non_null_values = [value for value in observed_values if value is not None]
    value_counts = Counter(_json_key(value) for value in non_null_values)
    values_by_key = {_json_key(value): value for value in non_null_values}
    high_frequency_values = [
        {
            "value": values_by_key[key],
            "count": count,
        }
        for key, count in value_counts.most_common(5)
    ]
    observed_count = len(observed_values)
    null_count = sum(1 for value in observed_values if value is None)
    return ColumnSampleStats(
        sample_row_count=len(sample_rows),
        observed_count=observed_count,
        non_null_count=len(non_null_values),
        null_count=null_count,
        distinct_count=len(value_counts),
        null_ratio=(null_count / observed_count) if observed_count else 0.0,
        high_frequency_values=high_frequency_values,
    )


def build_column_snapshot_evidence(
    *,
    table: TableMetadata,
    column_name: str,
    column_type: str,
    description: str,
    sample_values_per_column: int,
) -> ColumnSnapshotEvidence:
    return ColumnSnapshotEvidence(
        column_fullname=f"{table.full_name}.{column_name}",
        column_name=column_name,
        data_type=column_type,
        description=description,
        sample_values=sample_column_values(
            table.sample_rows,
            column_name,
            limit=sample_values_per_column,
        ),
        sample_stats=build_column_sample_stats(
            sample_rows=table.sample_rows,
            column_name=column_name,
        ),
    )


def build_relation_clues_from_explicit_constraints(
    *,
    table_fullname: str,
    explicit_constraints: dict[str, Any],
) -> list[DataSnapshotRelationClue]:
    clues: list[DataSnapshotRelationClue] = []
    for foreign_key in explicit_constraints.get("foreign_keys") or []:
        if not isinstance(foreign_key, dict):
            continue
        columns = [
            str(column or "").strip()
            for column in foreign_key.get("columns") or []
            if str(column or "").strip()
        ]
        referenced_columns = [
            str(column or "").strip()
            for column in foreign_key.get("referenced_columns") or []
            if str(column or "").strip()
        ]
        referenced_table = str(foreign_key.get("referenced_table") or "").strip()
        if not columns or not referenced_table:
            continue
        clues.append(
            DataSnapshotRelationClue(
                source_table=table_fullname,
                source_column=", ".join(columns),
                target_table=referenced_table,
                target_column=", ".join(referenced_columns),
                clue_type="explicit_foreign_key",
                confidence="high",
            )
        )
    return clues


def build_data_snapshot_from_table(
    table: TableMetadata,
    *,
    group: TableGroup | None = None,
    sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
) -> DataSnapshotEvidence:
    namespace, table_name = _split_table_fullname(table.full_name)
    columns = [
        build_column_snapshot_evidence(
            table=table,
            column_name=column_name,
            column_type=column_type,
            description=description,
            sample_values_per_column=sample_values_per_column,
        )
        for column_name, column_type, description in zip(
            table.column_names,
            table.column_types,
            table.descriptions,
        )
    ]
    quality_warnings: list[str] = []
    if not table.column_names:
        quality_warnings.append("No columns were found in the schema snapshot.")
    if not table.sample_rows:
        quality_warnings.append("No sample rows were found in the schema snapshot.")

    member_tables = [member.full_name for member in group.members] if group else [table.full_name]
    snapshot_id = group.group_id if group else safe_file_stem(table.full_name)
    column_stats = {
        column.column_name: column.sample_stats.to_dict() if column.sample_stats else {}
        for column in columns
    }
    explicit_constraints = normalize_explicit_constraints(table.explicit_constraints or {})
    relation_clues = build_relation_clues_from_explicit_constraints(
        table_fullname=table.full_name,
        explicit_constraints=explicit_constraints,
    )
    return DataSnapshotEvidence(
        snapshot_id=snapshot_id,
        source_kind="structured_table_group" if group and len(group.members) > 1 else "structured_table",
        table_fullname=table.full_name,
        namespace=table.namespace or namespace,
        table_name=table.short_name or table_name,
        snapshot_path=table.snapshot_path,
        table_description=table.table_description,
        group_id=group.group_id if group else "",
        grouping_method=GROUPING_METHOD if group else "",
        member_tables=member_tables,
        columns=columns,
        sample_rows=list(table.sample_rows),
        name_evidence={
            "table_fullname": table.full_name,
            "table_name": table.short_name or table_name,
            "member_tables": member_tables,
            "column_names": list(table.column_names),
        },
        structure_evidence={
            "column_count": len(table.column_names),
            "columns": [
                {
                    "column_name": column.column_name,
                    "data_type": column.data_type,
                    "description": column.description,
                }
                for column in columns
            ],
        },
        content_evidence={
            "sample_row_count": len(table.sample_rows),
            "sample_rows": list(table.sample_rows),
        },
        statistic_evidence={
            "column_stats": column_stats,
        },
        explicit_constraints=explicit_constraints,
        context_evidence={
            "source_snapshot_path": table.snapshot_path,
            "table_family_size": len(member_tables),
        },
        relation_clues=relation_clues,
        quality_warnings=quality_warnings,
    )


def build_data_snapshots_from_table_groups(
    table_groups: list[TableGroup],
    *,
    sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
) -> list[DataSnapshotEvidence]:
    return [
        build_data_snapshot_from_table(
            group.representative,
            group=group,
            sample_values_per_column=sample_values_per_column,
        )
        for group in table_groups
    ]


def build_data_snapshots_from_tables(
    tables: list[TableMetadata],
    *,
    group_table_families: bool = True,
    sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
) -> list[DataSnapshotEvidence]:
    if group_table_families:
        return build_data_snapshots_from_table_groups(
            build_table_groups(tables),
            sample_values_per_column=sample_values_per_column,
        )
    return [
        build_data_snapshot_from_table(
            table,
            sample_values_per_column=sample_values_per_column,
        )
        for table in tables
    ]


def load_data_snapshots(
    *,
    db_id: str,
    database_root: str | Path | None = None,
    spider2_root: str | Path | None = None,
    group_table_families: bool = True,
    sample_values_per_column: int = DEFAULT_SAMPLE_VALUES_PER_COLUMN,
) -> list[DataSnapshotEvidence]:
    tables = load_database_tables(
        db_id=db_id,
        database_root=database_root,
        spider2_root=spider2_root,
    )
    return build_data_snapshots_from_tables(
        tables,
        group_table_families=group_table_families,
        sample_values_per_column=sample_values_per_column,
    )
