from src.run.nl2er_only import BatchInput
from src.run.schema_linking_only import (
    SNAPSHOT_MODE_LINKED_COLUMNS,
    SNAPSHOT_MODE_LINKED_TABLE_ALL_COLUMNS,
    build_output_payload,
    build_merged_schema_linking_payload,
    format_quiet_coverage_report,
    project_schema_snapshot,
)
from src.validation.schema_linking_coverage import (
    BatchCoverageCaseResult,
    BatchCoverageReport,
    evaluate_schema_linking_coverage,
)


def test_project_schema_snapshot_defaults_to_linked_columns_only() -> None:
    evidence = {
        "db_id": "db1",
        "snapshot_search_roots": ["databases/db1"],
        "tables": [
            {
                "table_fullname": "db1.public.orders",
                "table_name": "orders",
                "description": "orders table",
                "snapshot_path": "orders.json",
                "linked_columns": [
                    {
                        "column_fullname": "db1.public.orders.order_id",
                        "column_name": "order_id",
                    }
                ],
                "available_columns": [
                    {
                        "column_fullname": "db1.public.orders.order_id",
                        "column_name": "order_id",
                    },
                    {
                        "column_fullname": "db1.public.orders.customer_id",
                        "column_name": "customer_id",
                    },
                ],
                "sample_rows": [
                    {"order_id": 1, "customer_id": 10},
                ],
            }
        ],
    }

    snapshot = project_schema_snapshot(
        evidence_payload=evidence,
        snapshot_mode=SNAPSHOT_MODE_LINKED_COLUMNS,
    )

    assert snapshot["column_scope"] == "linked_columns_only"
    assert snapshot["column_count"] == 1
    assert snapshot["tables"][0]["columns"] == [
        {
            "column_fullname": "db1.public.orders.order_id",
            "column_name": "order_id",
        }
    ]
    assert snapshot["tables"][0]["sample_rows"] == [{"order_id": 1}]


def test_project_schema_snapshot_can_include_all_linked_table_columns() -> None:
    evidence = {
        "db_id": "db1",
        "tables": [
            {
                "table_fullname": "db1.public.orders",
                "table_name": "orders",
                "available_columns": [
                    {"column_fullname": "db1.public.orders.order_id", "column_name": "order_id"},
                    {
                        "column_fullname": "db1.public.orders.customer_id",
                        "column_name": "customer_id",
                        "is_linked_column": False,
                    },
                ],
                "linked_columns": [
                    {"column_fullname": "db1.public.orders.order_id", "column_name": "order_id"}
                ],
                "sample_rows": [{"order_id": 1, "customer_id": 10}],
            }
        ],
    }

    snapshot = project_schema_snapshot(
        evidence_payload=evidence,
        snapshot_mode=SNAPSHOT_MODE_LINKED_TABLE_ALL_COLUMNS,
    )

    assert snapshot["column_scope"] == "all_columns_from_linked_tables"
    assert snapshot["column_count"] == 2
    assert snapshot["tables"][0]["columns"] == [
        {"column_fullname": "db1.public.orders.order_id", "column_name": "order_id"},
        {"column_fullname": "db1.public.orders.customer_id", "column_name": "customer_id"},
    ]
    assert snapshot["tables"][0]["sample_rows"] == [{"order_id": 1, "customer_id": 10}]


def test_build_output_payload_keeps_linking_fields_for_coverage() -> None:
    output = build_output_payload(
        input_payload=BatchInput(
            question_id="q1",
            user_intent="show orders",
            db_id="db1",
        ),
        engine_result_payload={
            "ok": True,
            "engine": "engine",
            "result_path": "result.json",
            "tables": [{"fullname": "db1.public.orders"}],
            "columns": [{"fullname": "db1.public.orders.order_id"}],
        },
        schema_snapshot_payload={"tables": []},
        snapshot_mode=SNAPSHOT_MODE_LINKED_COLUMNS,
    )

    assert output["question"]["linked_tables"] == ["db1.public.orders"]
    assert output["question"]["linked_columns"] == ["db1.public.orders.order_id"]
    assert output["question"]["schema_snapshot"] == {"tables": []}


def test_format_quiet_coverage_report_prints_each_question_metrics() -> None:
    coverage = evaluate_schema_linking_coverage(
        schema_linking_payload={
            "linked_tables": ["sales.analytics.orders"],
            "linked_columns": ["sales.analytics.orders.order_id"],
        },
        ground_truth_sql="""
SELECT
    o.order_id,
    c.customer_name
FROM sales.analytics.orders AS o
JOIN sales.analytics.customers AS c
    ON o.customer_id = c.customer_id
""",
        dialect="snowflake",
    )
    report = BatchCoverageReport(
        metadata_dir="metadata/run",
        ground_truth_dir="data/ground_truth",
        total_cases=1,
        ok_cases=1,
        covered_cases=0,
        uncovered_cases=1,
        error_cases=0,
        strict_table_recall_rate=1.0 if coverage.table_coverage.recall == 1.0 else 0.0,
        strict_column_recall_rate=(
            1.0 if coverage.resolved_column_coverage.recall == 1.0 else 0.0
        ),
        cases=[
            BatchCoverageCaseResult(
                question_id="sf_test",
                case_dir="metadata/run/sf_test",
                schema_linking_path="metadata/run/sf_test/schema_linking.json",
                ground_truth_path="data/ground_truth/sf_test.sql",
                ground_truth_source="ground_truth_dir",
                dialect="snowflake",
                ok=True,
                coverage=coverage,
            )
        ],
    )

    rendered = format_quiet_coverage_report(
        report,
        report_path="metadata/run/schema_linking_coverage_report.txt",
    )

    assert "sf_test table_precision=" in rendered
    assert "strict_table_recall_rate=" in rendered
    assert "strict_column_recall_rate=" in rendered
    assert "table_recall=" in rendered
    assert "column_precision=" in rendered
    assert "column_recall=" in rendered
    assert "sf_test missing_tables=SALES.ANALYTICS.CUSTOMERS" in rendered
    assert (
        "sf_test missing_columns=SALES.ANALYTICS.CUSTOMERS.CUSTOMER_ID, "
        "SALES.ANALYTICS.CUSTOMERS.CUSTOMER_NAME, "
        "SALES.ANALYTICS.ORDERS.CUSTOMER_ID"
    ) in rendered
    assert "coverage_report=metadata/run/schema_linking_coverage_report.txt" in rendered


def test_build_merged_schema_linking_payload_combines_question_and_unit_results() -> None:
    payload = build_merged_schema_linking_payload(
        input_payload=BatchInput(
            question_id="q1",
            user_intent="show orders by customer",
            db_id="db1",
        ),
        question_schema_linking_payload={
            "question": {
                "linked_tables": ["db1.public.orders"],
                "linked_columns": ["db1.public.orders.order_id"],
            }
        },
        unit_schema_linking_payload={
            "customer_unit": {
                "linked_tables": ["db1.public.customers"],
                "linked_columns": ["db1.public.customers.customer_id"],
            }
        },
        snapshot_mode=SNAPSHOT_MODE_LINKED_COLUMNS,
        sample_values_per_column=1,
        spider2_root=None,
    )

    assert payload["question"]["linked_tables"] == [
        "db1.public.customers",
        "db1.public.orders",
    ]
    assert payload["question"]["linked_columns"] == [
        "db1.public.customers.customer_id",
        "db1.public.orders.order_id",
    ]
    assert payload["sources"]["whole_question"]
    assert payload["sources"]["er_units"]
    assert payload["units"]["customer_unit"]["linked_tables"] == ["db1.public.customers"]
