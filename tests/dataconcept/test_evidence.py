from __future__ import annotations

from src.dataconcept.evidence import (
    build_data_snapshot_from_table,
    build_data_snapshots_from_tables,
    load_data_snapshots,
)
from src.er2data.physical_schema import TableMetadata


def make_table(
    *,
    full_name: str,
    columns: list[str],
    types: list[str] | None = None,
    descriptions: list[str] | None = None,
    sample_rows: list[dict] | None = None,
    explicit_constraints: dict | None = None,
) -> TableMetadata:
    parts = full_name.split(".")
    return TableMetadata(
        full_name=full_name,
        namespace=".".join(parts[:-1]),
        short_name=parts[-1],
        column_names=columns,
        column_types=types or [""] * len(columns),
        descriptions=descriptions or [""] * len(columns),
        sample_rows=sample_rows or [],
        snapshot_path=f"databases/test/{parts[-1]}.json",
        explicit_constraints=explicit_constraints,
    )


def test_build_data_snapshot_from_single_table_preserves_schema_and_samples() -> None:
    table = make_table(
        full_name="db.public.orders",
        columns=["order_id", "customer_id", "amount", "created_at"],
        types=["NUMBER", "NUMBER", "NUMBER", "TIMESTAMP"],
        sample_rows=[
            {"order_id": 1, "customer_id": 10, "amount": 25.5, "created_at": "2026-01-01"},
            {"order_id": 2, "customer_id": 11, "amount": 42.0, "created_at": "2026-01-02"},
        ],
    )

    snapshot = build_data_snapshot_from_table(table)

    assert snapshot.source_kind == "structured_table"
    assert snapshot.table_fullname == "db.public.orders"
    assert snapshot.structure_evidence["column_count"] == 4
    assert snapshot.content_evidence["sample_row_count"] == 2
    assert snapshot.columns[0].column_fullname == "db.public.orders.order_id"
    assert snapshot.columns[0].sample_values == [1, 2]
    assert snapshot.statistic_evidence == {
        "column_stats": {
            column.column_name: column.sample_stats.to_dict()
            for column in snapshot.columns
        }
    }
    assert snapshot.relation_clues == []
    assert snapshot.quality_warnings == []


def test_column_sample_stats_count_nulls_and_distinct_values() -> None:
    table = make_table(
        full_name="db.public.customers",
        columns=["customer_id", "status"],
        sample_rows=[
            {"customer_id": 1, "status": "active"},
            {"customer_id": 2, "status": "active"},
            {"customer_id": 3, "status": None},
        ],
    )

    snapshot = build_data_snapshot_from_table(table)
    status_stats = snapshot.columns[1].sample_stats

    assert status_stats is not None
    assert status_stats.sample_row_count == 3
    assert status_stats.observed_count == 3
    assert status_stats.non_null_count == 2
    assert status_stats.null_count == 1
    assert status_stats.distinct_count == 1
    assert status_stats.null_ratio == 1 / 3
    assert status_stats.high_frequency_values == [{"value": "active", "count": 2}]


def test_quality_warning_when_snapshot_has_no_sample_rows() -> None:
    table = make_table(
        full_name="db.public.empty_sample",
        columns=["item_id", "item_name"],
    )

    snapshot = build_data_snapshot_from_table(table)

    assert "No sample rows were found in the schema snapshot." in snapshot.quality_warnings


def test_build_data_snapshots_from_table_family_records_members() -> None:
    tables = [
        make_table(
            full_name="db.public.orders_2021",
            columns=["order_id", "customer_id"],
            types=["NUMBER", "NUMBER"],
            sample_rows=[{"order_id": 1, "customer_id": 10}],
        ),
        make_table(
            full_name="db.public.orders_2022",
            columns=["order_id", "customer_id"],
            types=["NUMBER", "NUMBER"],
            sample_rows=[{"order_id": 2, "customer_id": 20}],
        ),
    ]

    snapshots = build_data_snapshots_from_tables(tables)

    assert len(snapshots) == 1
    assert snapshots[0].source_kind == "structured_table_group"
    assert snapshots[0].group_id.startswith("group_")
    assert snapshots[0].member_tables == ["db.public.orders_2021", "db.public.orders_2022"]
    assert snapshots[0].context_evidence["table_family_size"] == 2


def test_data_snapshot_does_not_infer_relation_clues_from_column_names() -> None:
    tables = [
        make_table(
            full_name="db.public.person",
            columns=["person_id", "name"],
            sample_rows=[{"person_id": "p1", "name": "Ann"}],
        ),
        make_table(
            full_name="db.public.contact",
            columns=["contact_id", "person_id", "phone"],
            sample_rows=[{"contact_id": "c1", "person_id": "p1", "phone": "123"}],
        ),
    ]

    snapshots = build_data_snapshots_from_tables(tables, group_table_families=False)
    contact_snapshot = next(item for item in snapshots if item.table_name == "contact")

    assert contact_snapshot.relation_clues == []


def test_data_snapshot_preserves_explicit_primary_and_foreign_keys() -> None:
    table = make_table(
        full_name="db.public.orders",
        columns=["order_id", "customer_id"],
        sample_rows=[{"order_id": 1, "customer_id": 10}],
        explicit_constraints={
            "primary_keys": ["order_id"],
            "unique_keys": [["order_id", "customer_id"]],
            "foreign_keys": [
                {
                    "columns": ["customer_id"],
                    "referenced_table": "db.public.customers",
                    "referenced_columns": ["customer_id"],
                }
            ],
            "indexes": [{"name": "idx_orders_customer", "columns": ["customer_id"]}],
        },
    )

    snapshot = build_data_snapshot_from_table(table)

    assert snapshot.explicit_constraints["primary_keys"] == ["order_id"]
    assert snapshot.explicit_constraints["unique_keys"] == [["order_id", "customer_id"]]
    assert snapshot.explicit_constraints["indexes"] == [
        {"name": "idx_orders_customer", "columns": ["customer_id"], "is_unique": False}
    ]
    assert [clue.to_dict() for clue in snapshot.relation_clues] == [
        {
            "source_table": "db.public.orders",
            "source_column": "customer_id",
            "target_table": "db.public.customers",
            "target_column": "customer_id",
            "clue_type": "explicit_foreign_key",
            "confidence": "high",
        }
    ]


def test_load_data_snapshots_reads_existing_database_root() -> None:
    snapshots = load_data_snapshots(
        db_id="sy_community_link",
        database_root="databases",
        group_table_families=False,
        sample_values_per_column=2,
    )

    person_snapshot = next(
        item for item in snapshots if item.table_fullname == "sy_community_link.ads_person_basic_info"
    )

    assert person_snapshot.table_name == "ads_person_basic_info"
    assert person_snapshot.columns[0].column_name == "person_id"
    assert len(person_snapshot.columns[0].sample_values) == 2
    assert person_snapshot.content_evidence["sample_row_count"] > 0
