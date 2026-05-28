from __future__ import annotations

import pytest

from src.validation.schema_linking_coverage import (
    evaluate_schema_linking_coverage,
    extract_schema_linking_selection,
    format_batch_coverage_report,
    BatchCoverageCaseResult,
    BatchCoverageReport,
    compute_strict_recall_rates,
)


def test_extract_schema_linking_selection_supports_compact_unit_payload() -> None:
    payload = {
        "entity_orders": {
            "linked_tables": [
                "sales.analytics.orders",
                "sales.analytics.customers",
            ],
            "linked_columns": [
                "sales.analytics.orders.order_id",
                "sales.analytics.customers.customer_id",
            ],
        },
        "relationship_orders_customers": {
            "linked_tables": [
                "sales.analytics.customers",
                "sales.analytics.regions",
            ],
            "linked_columns": [
                "sales.analytics.customers.region_id",
                "sales.analytics.regions.region_name",
            ],
        },
    }

    selection = extract_schema_linking_selection(payload)

    assert selection.tables == [
        "SALES.ANALYTICS.CUSTOMERS",
        "SALES.ANALYTICS.ORDERS",
        "SALES.ANALYTICS.REGIONS",
    ]
    assert selection.columns == [
        "SALES.ANALYTICS.CUSTOMERS.CUSTOMER_ID",
        "SALES.ANALYTICS.CUSTOMERS.REGION_ID",
        "SALES.ANALYTICS.ORDERS.ORDER_ID",
        "SALES.ANALYTICS.REGIONS.REGION_NAME",
    ]
    assert selection.column_names == [
        "CUSTOMER_ID",
        "ORDER_ID",
        "REGION_ID",
        "REGION_NAME",
    ]


def test_extract_schema_linking_selection_supports_reforce_raw_payload() -> None:
    payload = {
        "question": {
            "unit_type": "question",
            "raw_result": {
                "parsed_info": {
                    "gen_tb": [
                        "warehouse.reporting.orders",
                    ],
                    "gen_col": [
                        "warehouse.reporting.orders.order_id",
                        "warehouse.reporting.orders.customer_id",
                    ],
                }
            },
        }
    }

    selection = extract_schema_linking_selection(payload)

    assert selection.tables == ["WAREHOUSE.REPORTING.ORDERS"]
    assert selection.columns == [
        "WAREHOUSE.REPORTING.ORDERS.CUSTOMER_ID",
        "WAREHOUSE.REPORTING.ORDERS.ORDER_ID",
    ]
    assert selection.column_names == ["CUSTOMER_ID", "ORDER_ID"]


def test_evaluate_schema_linking_coverage_handles_declare_and_cte() -> None:
    schema_linking_payload = {
        "linked_tables": [
            "sales.analytics.orders",
            "sales.analytics.customers",
        ],
        "linked_columns": [
            "sales.analytics.orders.order_id",
            "sales.analytics.orders.customer_id",
            "sales.analytics.customers.customer_id",
            "sales.analytics.customers.customer_name",
        ],
    }
    ground_truth_sql = """
DECLARE metric STRING;
WITH recent_orders AS (
    SELECT
        o.order_id,
        o.customer_id
    FROM sales.analytics.orders AS o
)
SELECT
    r.order_id,
    c.customer_name
FROM recent_orders AS r
JOIN sales.analytics.customers AS c
    ON r.customer_id = c.customer_id
"""

    result = evaluate_schema_linking_coverage(
        schema_linking_payload=schema_linking_payload,
        ground_truth_sql=ground_truth_sql,
        dialect="snowflake",
    )

    assert result.fully_covered is True
    assert result.gold.tables == [
        "SALES.ANALYTICS.CUSTOMERS",
        "SALES.ANALYTICS.ORDERS",
    ]
    assert "RECENT_ORDERS" not in result.gold.tables
    assert result.table_coverage.missing == []
    assert result.column_name_coverage.missing == []
    assert result.resolved_column_coverage.missing == []


def test_evaluate_schema_linking_coverage_supports_raw_provider_payload() -> None:
    schema_linking_payload = {
        "schema_linking": {
            "tables": [
                {
                    "fullname": "warehouse.reporting.orders",
                    "database": "warehouse",
                    "schema": "reporting",
                    "table": "orders",
                }
            ],
            "columns": [
                {
                    "fullname": "warehouse.reporting.orders.order_id",
                    "database": "warehouse",
                    "schema": "reporting",
                    "table": "orders",
                    "column": "order_id",
                }
            ],
        }
    }
    ground_truth_sql = "SELECT order_id FROM warehouse.reporting.orders"

    result = evaluate_schema_linking_coverage(
        schema_linking_payload=schema_linking_payload,
        ground_truth_sql=ground_truth_sql,
        dialect="snowflake",
    )

    assert result.fully_covered is True
    assert result.table_coverage.recall == pytest.approx(1.0)
    assert result.column_name_coverage.recall == pytest.approx(1.0)
    assert result.resolved_column_coverage.recall == pytest.approx(1.0)


def test_evaluate_schema_linking_coverage_reports_missing_items() -> None:
    schema_linking_payload = {
        "linked_tables": [
            "sales.analytics.orders",
        ],
        "linked_columns": [
            "sales.analytics.orders.order_id",
        ],
    }
    ground_truth_sql = """
SELECT
    o.order_id,
    c.customer_name
FROM sales.analytics.orders AS o
JOIN sales.analytics.customers AS c
    ON o.customer_id = c.customer_id
"""

    result = evaluate_schema_linking_coverage(
        schema_linking_payload=schema_linking_payload,
        ground_truth_sql=ground_truth_sql,
        dialect="snowflake",
    )

    assert result.fully_covered is False
    assert result.table_coverage.missing == ["SALES.ANALYTICS.CUSTOMERS"]
    assert result.column_name_coverage.missing == ["CUSTOMER_ID", "CUSTOMER_NAME"]
    assert "SALES.ANALYTICS.CUSTOMERS.CUSTOMER_ID" in result.resolved_column_coverage.missing
    assert "SALES.ANALYTICS.ORDERS.CUSTOMER_ID" in result.resolved_column_coverage.missing


def test_evaluate_schema_linking_coverage_ignores_select_alias_references() -> None:
    schema_linking_payload = {
        "linked_tables": ["demo.analytics.metrics"],
        "linked_columns": [
            "demo.analytics.metrics.created_at",
            "demo.analytics.metrics.user_id",
        ],
    }
    ground_truth_sql = """
SELECT
    DATE_TRUNC('day', m.created_at) AS day_bucket,
    COUNT(DISTINCT m.user_id) AS user_count
FROM demo.analytics.metrics AS m
GROUP BY day_bucket
ORDER BY user_count DESC
"""

    result = evaluate_schema_linking_coverage(
        schema_linking_payload=schema_linking_payload,
        ground_truth_sql=ground_truth_sql,
        dialect="snowflake",
    )

    assert result.fully_covered is True
    assert result.resolved_column_coverage.missing == []
    assert "DEMO.ANALYTICS.METRICS.DAY_BUCKET" not in result.gold.resolved_columns
    assert "DEMO.ANALYTICS.METRICS.USER_COUNT" not in result.gold.resolved_columns


def test_fully_covered_uses_displayed_table_and_table_column_recall() -> None:
    schema_linking_payload = {
        "linked_tables": [
            "sales.analytics.orders",
            "sales.analytics.customers",
        ],
        "linked_columns": [
            "sales.analytics.orders.order_id",
            "sales.analytics.orders.customer_name",
            "sales.analytics.orders.customer_id",
        ],
    }
    ground_truth_sql = """
SELECT
    o.order_id,
    c.customer_name
FROM sales.analytics.orders AS o
JOIN sales.analytics.customers AS c
    ON o.customer_id = c.customer_id
"""

    result = evaluate_schema_linking_coverage(
        schema_linking_payload=schema_linking_payload,
        ground_truth_sql=ground_truth_sql,
        dialect="snowflake",
    )

    assert result.table_coverage.recall == pytest.approx(1.0)
    assert result.resolved_column_coverage.recall < 1.0
    assert result.fully_covered is False


def test_format_batch_coverage_report_only_shows_table_column_metrics() -> None:
    schema_linking_payload = {
        "linked_tables": ["sales.analytics.orders"],
        "linked_columns": ["sales.analytics.orders.order_id"],
    }
    coverage = evaluate_schema_linking_coverage(
        schema_linking_payload=schema_linking_payload,
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

    rendered = format_batch_coverage_report(report)

    assert "table_precision=" in rendered
    assert "table_recall=" in rendered
    assert "column_precision=" in rendered
    assert "column_recall=" in rendered
    assert "strict_table_recall_rate=" in rendered
    assert "strict_column_recall_rate=" in rendered
    assert "missing_tables:" in rendered
    assert "missing_columns:" in rendered
    assert "resolved_col" not in rendered
    assert "missing_resolved_columns" not in rendered
    assert "missing_col_names" not in rendered


def test_compute_strict_recall_rates_counts_only_full_recall_cases() -> None:
    full_coverage = evaluate_schema_linking_coverage(
        schema_linking_payload={
            "linked_tables": ["sales.analytics.orders"],
            "linked_columns": ["sales.analytics.orders.order_id"],
        },
        ground_truth_sql="SELECT order_id FROM sales.analytics.orders",
        dialect="snowflake",
    )
    partial_coverage = evaluate_schema_linking_coverage(
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
    case_results = [
        BatchCoverageCaseResult(
            question_id="q1",
            case_dir="metadata/q1",
            schema_linking_path="metadata/q1/schema_linking.json",
            ground_truth_path="ground_truth/q1.sql",
            ground_truth_source="ground_truth_dir",
            dialect="snowflake",
            ok=True,
            coverage=full_coverage,
        ),
        BatchCoverageCaseResult(
            question_id="q2",
            case_dir="metadata/q2",
            schema_linking_path="metadata/q2/schema_linking.json",
            ground_truth_path="ground_truth/q2.sql",
            ground_truth_source="ground_truth_dir",
            dialect="snowflake",
            ok=True,
            coverage=partial_coverage,
        ),
    ]

    table_srr, column_srr = compute_strict_recall_rates(case_results)

    assert table_srr == pytest.approx(0.5)
    assert column_srr == pytest.approx(0.5)
