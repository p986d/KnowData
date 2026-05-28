import json
import shutil
import uuid
from pathlib import Path

from src.er2data.physical_schema import TableMetadata
from src.er2data.table_grouping import build_table_groups, group_payloads_from_groups
from src.nl2er.concept_extraction import build_compact_concept_output, normalize_concept_profile
from src.run.database_table_concept_extraction import DatabaseTableConceptExtractionRunner


def test_normalize_concept_profile_collects_concepts_and_evidence_columns() -> None:
    tables = [
        TableMetadata(
            full_name="db.public.orders",
            namespace="db.public",
            short_name="orders",
            column_names=["order_id", "customer_id", "created_at"],
            column_types=["NUMBER", "NUMBER", "TIMESTAMP"],
            descriptions=["", "", ""],
            sample_rows=[],
            snapshot_path="",
        )
    ]
    payload = group_payloads_from_groups(
        build_table_groups(tables),
        sample_row_limit=1,
        sample_values_per_column=1,
        sample_value_max_chars=100,
    )[0]

    profile = normalize_concept_profile(
        parsed={
            "table_concept_profile": {
                "concepts": [
                    {
                        "concept_name": "Order",
                        "concept_type": "entity",
                        "identifier_columns": ["order_id"],
                        "attribute_columns": [
                            {"column": "customer_id", "data_role": "foreign_key"}
                        ],
                    }
                ],
                "foreign_key_candidates": [
                    {
                        "columns": ["customer_id"],
                        "referenced_table": "Customer",
                    }
                ],
                "metadata_columns": ["created_at"],
            }
        },
        item=payload,
    )

    assert profile["representative_table"] == "db.public.orders"
    assert profile["concepts"][0]["concept_name"] == "Order"
    assert profile["foreign_key_candidates"][0]["referenced_concept_or_table"] == "Customer"
    assert profile["evidence_columns"] == ["created_at", "order_id", "customer_id"]


def test_compact_concept_output_keeps_source_table_group() -> None:
    compact = build_compact_concept_output(
        [
            {
                "group_id": "group_1",
                "representative_table": "db.public.orders",
                "member_tables": ["db.public.orders"],
                "concepts": [
                    {
                        "concept_name": "Order",
                        "concept_type": "entity",
                        "grain": "one order",
                    }
                ],
                "metadata_columns": ["updated_at"],
            }
        ]
    )

    assert compact["concepts"][0]["source_table_group"] == "group_1"
    assert compact["metadata_columns_by_table"] == [
        {
            "representative_table": "db.public.orders",
            "member_tables": ["db.public.orders"],
            "metadata_columns": ["updated_at"],
        }
    ]


def test_concept_prompt_is_question_independent() -> None:
    tmp_path = Path("tests/_tmp") / f"concept_prompt_{uuid.uuid4().hex}"
    try:
        runner = DatabaseTableConceptExtractionRunner(
            prompt_dir="src/prompt/prompt_template",
            log_dir=tmp_path / "log",
            dry_run=True,
        )
        tables = [
            TableMetadata(
                full_name="db.public.orders",
                namespace="db.public",
                short_name="orders",
                column_names=["order_id", "customer_id"],
                column_types=["NUMBER", "NUMBER"],
                descriptions=["", ""],
                sample_rows=[],
                snapshot_path="",
            )
        ]
        payloads = group_payloads_from_groups(
            build_table_groups(tables),
            sample_row_limit=1,
            sample_values_per_column=1,
            sample_value_max_chars=100,
        )

        _items, prompts = runner.prepare_group_prompts(
            context={"db_id": "db", "db_hint": ""},
            group_payloads=payloads,
        )

        assert "Target Table Group" in prompts[0]
        assert "Original User Question" not in prompts[0]
        assert "user_intent" not in prompts[0]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
