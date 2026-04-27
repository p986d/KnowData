from __future__ import annotations

import pytest

from src.validation.schema_linking_coverage import (
    evaluate_schema_linking_coverage,
    extract_schema_linking_selection,
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
