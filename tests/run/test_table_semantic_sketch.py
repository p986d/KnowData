import argparse
import json
import shutil
import uuid
from pathlib import Path

from src.run.schema_linking_table_semantic_sketch import (
    CaseInput,
    TableSemanticSketchRunner,
    build_schema_linking_with_overall_result,
    build_target_table_prompt_snapshot,
    collect_output_schema_selection,
    normalize_model_profile,
    resolve_table_sketch_concurrency,
)
from src.run.database_table_semantic_sketch import (
    DatabaseCaseInput,
    DatabaseTableSemanticSketchRunner,
    TableMetadata,
    build_table_groups,
    collect_group_schema_selection,
    discover_database_case_inputs,
    group_payloads_from_groups,
    normalize_context,
)


def test_collect_output_schema_selection_uses_profile_evidence_columns() -> None:
    table_payloads = [
        {
            "table_fullname": "db.public.orders",
            "linked_columns": ["db.public.orders.order_id"],
            "target_table": {
                "available_columns": [
                    {
                        "column_fullname": "db.public.orders.customer_id",
                        "column_name": "customer_id",
                    },
                ],
            },
        },
        {
            "table_fullname": "db.public.customers",
            "linked_columns": [],
        },
    ]
    sketches = [
        {
            "table_fullname": "db.public.orders",
            "semantic_units": [
                {
                    "attributes": [
                        {
                            "name": "customer",
                            "evidence_columns": [
                                "customer_id",
                                "unresolved_bare_name",
                            ],
                        }
                    ]
                }
            ],
        },
        {
            "table_fullname": "db.public.customers",
            "semantic_units": [
                {
                    "participants": [
                        {
                            "evidence_columns": [
                                "db.public.customers.customer_id",
                                "db.public.orders.customer_id",
                            ]
                        }
                    ]
                }
            ],
        },
    ]

    tables, columns = collect_output_schema_selection(
        table_payloads=table_payloads,
        sketches=sketches,
    )

    assert tables == ["db.public.orders", "db.public.customers"]
    assert columns == [
        "db.public.orders.order_id",
        "db.public.orders.customer_id",
        "db.public.customers.customer_id",
    ]


def test_build_schema_linking_with_overall_result_adds_question_selection() -> None:
    payload = build_schema_linking_with_overall_result(
        schema_linking_payload={
            "Bike Trip": {
                "linked_tables": ["db.public.orders"],
                "linked_columns": ["db.public.orders.order_id"],
            }
        },
        context={
            "question_id": "q1",
            "db_id": "db",
            "user_intent": "show orders",
        },
        linked_tables=["db.public.orders"],
        linked_columns=["db.public.orders.order_id", "db.public.orders.customer_id"],
        source="table_semantic_sketch_output",
    )

    assert payload["Bike Trip"]["linked_tables"] == ["db.public.orders"]
    assert payload["question"]["unit_name"] == "question"
    assert payload["question"]["unit_type"] == "question"
    assert payload["question"]["question_id"] == "q1"
    assert payload["question"]["db_id"] == "db"
    assert payload["question"]["question"] == "show orders"
    assert payload["question"]["linked_tables"] == ["db.public.orders"]
    assert payload["question"]["linked_columns"] == [
        "db.public.orders.order_id",
        "db.public.orders.customer_id",
    ]
    assert payload["question"]["schema_linking"] == {
        "ok": True,
        "source": "table_semantic_sketch_output",
        "table_count": 1,
        "column_count": 2,
    }


def test_normalize_model_profile_converts_legacy_units_to_new_format() -> None:
    profile = normalize_model_profile(
        parsed={
            "table_name": "orders",
            "semantics": "Supports order facts.",
            "units": [
                {
                    "unit_type": "entity",
                    "name": "Order",
                    "related_attributes": ["order_id"],
                    "evidence_columns": ["db.public.orders.order_id"],
                }
            ],
        },
        table_fullname="db.public.orders",
        target_table={},
    )

    assert profile["table_fullname"] == "db.public.orders"
    assert profile["supported_question_semantics"] == ["Supports order facts."]
    assert profile["is_relevant"] is True
    assert profile["semantic_units"][0]["unit_name"] == "Order"
    assert profile["semantic_units"][0]["attributes"] == [
        {
            "name": "order_id",
            "semantics": "",
            "evidence_columns": ["db.public.orders.order_id"],
        }
    ]


def test_resolve_table_sketch_concurrency_defaults_and_caps_global_batch_at_64() -> None:
    assert resolve_table_sketch_concurrency(None) == 64
    assert resolve_table_sketch_concurrency(32) == 32
    assert resolve_table_sketch_concurrency(64) == 64
    assert resolve_table_sketch_concurrency(128) == 64


def test_database_table_grouping_uses_reforce_family_key() -> None:
    tables = [
        TableMetadata(
            full_name="db.public.orders_2021",
            namespace="db.public",
            short_name="orders_2021",
            column_names=["order_id", "customer_id"],
            column_types=["NUMBER", "TEXT"],
            descriptions=["", ""],
            sample_rows=[],
            snapshot_path="",
        ),
        TableMetadata(
            full_name="db.public.orders_2022",
            namespace="db.public",
            short_name="orders_2022",
            column_names=["customer_id", "order_id"],
            column_types=["TEXT", "NUMBER"],
            descriptions=["", ""],
            sample_rows=[],
            snapshot_path="",
        ),
        TableMetadata(
            full_name="db.public.customers",
            namespace="db.public",
            short_name="customers",
            column_names=["customer_id"],
            column_types=["NUMBER"],
            descriptions=[""],
            sample_rows=[],
            snapshot_path="",
        ),
    ]

    groups = build_table_groups(tables)

    group_sizes = sorted(len(group.members) for group in groups)
    assert group_sizes == [1, 2]


def test_database_table_discovery_reads_question_input_file() -> None:
    tmp_path = Path("tests/_tmp") / f"database_table_input_{uuid.uuid4().hex}"
    tmp_path.mkdir(parents=True)
    try:
        input_path = tmp_path / "input.json"
        input_path.write_text(
            json.dumps(
                [
                    {
                        "question_id": "q1",
                        "db_id": "db1",
                        "user_intent": "show orders",
                        "db_hint": "hint",
                        "external_knowledge": "knowledge",
                    },
                    {
                        "question_id": "q2",
                        "db_id": "db2",
                        "user_intent": "show customers",
                    },
                ]
            ),
            encoding="utf-8",
        )

        batch_root, cases = discover_database_case_inputs(
            input_path=input_path,
            metadata_dir=None,
            output_path=None,
            output_filename="database_table_profile.json",
            requested_question_ids={"q2"},
        )

        assert batch_root == input_path.resolve()
        assert len(cases) == 1
        assert cases[0].relative_case_dir == Path("q2")
        assert cases[0].output_path is None
        assert cases[0].context == {
            "question_id": "q2",
            "user_intent": "show customers",
            "db_id": "db2",
            "db_hint": "",
            "external_knowledge": "",
        }
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_database_table_normalize_context_prefers_input_payload_context() -> None:
    tmp_path = Path("tests/_tmp") / f"database_table_input_{uuid.uuid4().hex}"
    tmp_path.mkdir(parents=True)
    try:
        case_input = DatabaseCaseInput(
            case_dir=tmp_path,
            relative_case_dir=Path("q1"),
            output_path=None,
            context={
                "question_id": "q1",
                "user_intent": "show orders",
                "db_id": "db1",
                "db_hint": "",
                "external_knowledge": "",
            },
        )

        context = normalize_context(
            case_input=case_input,
            args=type(
                "Args",
                (),
                {
                    "nl2er_output_path": None,
                    "nl2er_output_filename": "nl2er_output.json",
                    "question": None,
                    "db_id": None,
                    "db_hint": "",
                    "external_knowledge": "",
                    "question_id": None,
                },
            )(),
        )

        assert context["question_id"] == "q1"
        assert context["user_intent"] == "show orders"
        assert context["db_id"] == "db1"
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_collect_group_schema_selection_expands_representative_columns_to_members() -> None:
    tables = [
        TableMetadata(
            full_name="db.public.orders_2021",
            namespace="db.public",
            short_name="orders_2021",
            column_names=["order_id", "customer_id"],
            column_types=["NUMBER", "NUMBER"],
            descriptions=["", ""],
            sample_rows=[],
            snapshot_path="",
        ),
        TableMetadata(
            full_name="db.public.orders_2022",
            namespace="db.public",
            short_name="orders_2022",
            column_names=["order_id", "customer_id"],
            column_types=["NUMBER", "NUMBER"],
            descriptions=["", ""],
            sample_rows=[],
            snapshot_path="",
        ),
    ]
    groups = build_table_groups(tables)
    payloads = group_payloads_from_groups(
        groups,
        sample_row_limit=1,
        sample_values_per_column=1,
        sample_value_max_chars=100,
    )
    profiles = [
        {
            "is_relevant": True,
            "semantic_units": [
                {
                    "attributes": [
                        {
                            "name": "customer",
                            "evidence_columns": ["customer_id"],
                        }
                    ]
                }
            ],
        }
    ]

    linked_tables, linked_columns = collect_group_schema_selection(
        group_payloads=payloads,
        group_profiles=profiles,
    )

    assert linked_tables == ["db.public.orders_2021", "db.public.orders_2022"]
    assert linked_columns == [
        "db.public.orders_2021.customer_id",
        "db.public.orders_2022.customer_id",
    ]


def test_database_runner_uses_group_prompt_and_parses_group_response() -> None:
    tmp_path = Path("tests/_tmp") / f"database_table_semantic_{uuid.uuid4().hex}"
    try:
        runner = DatabaseTableSemanticSketchRunner(
            prompt_dir="src/prompt/prompt_template",
            log_dir=tmp_path / "log",
            dry_run=True,
        )
        groups = build_table_groups(
            [
                TableMetadata(
                    full_name="db.public.orders_2021",
                    namespace="db.public",
                    short_name="orders_2021",
                    column_names=["order_id", "customer_id"],
                    column_types=["NUMBER", "NUMBER"],
                    descriptions=["", ""],
                    sample_rows=[],
                    snapshot_path="",
                ),
                TableMetadata(
                    full_name="db.public.orders_2022",
                    namespace="db.public",
                    short_name="orders_2022",
                    column_names=["order_id", "customer_id"],
                    column_types=["NUMBER", "NUMBER"],
                    descriptions=["", ""],
                    sample_rows=[],
                    snapshot_path="",
                ),
            ]
        )
        payloads = group_payloads_from_groups(
            groups,
            sample_row_limit=1,
            sample_values_per_column=1,
            sample_value_max_chars=100,
        )
        items, prompts = runner.prepare_group_prompts(
            context={
                "user_intent": "show orders by customer",
                "sub_questions": "Find orders.",
                "db_id": "db",
                "db_hint": "",
                "external_knowledge": "",
            },
            group_payloads=payloads,
        )

        assert "Target Table Group" in prompts[0]
        assert "Find orders." in prompts[0]

        raw_response = json.dumps(
            {
                "group_id": groups[0].group_id,
                "representative_table": "db.public.orders_2021",
                "member_table_scope": "selected_members",
                "selected_member_tables": [
                    "db.public.orders_2021",
                    "db.public.orders_2022",
                ],
                "is_relevant": "true",
                "linked_columns": [
                    "db.public.orders_2021.customer_id",
                ],
                "semantic_units": [
                    {
                        "unit_type": "entity",
                        "unit_name": "Order",
                        "attributes": [
                            {
                                "name": "customer",
                                "evidence_columns": ["customer_id"],
                            }
                        ],
                        "participants": [],
                    }
                ],
            }
        )
        profiles = runner.parse_group_responses(items=items, raw_responses=[raw_response])

        assert profiles[0]["is_relevant"] is True
        assert profiles[0]["member_table_scope"] == "selected_members"
        linked_tables, linked_columns = collect_group_schema_selection(
            group_payloads=payloads,
            group_profiles=profiles,
        )
        assert linked_tables == ["db.public.orders_2021", "db.public.orders_2022"]
        assert linked_columns == [
            "db.public.orders_2021.customer_id",
            "db.public.orders_2022.customer_id",
        ]
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_build_target_table_prompt_snapshot_uses_plain_columns() -> None:
    snapshot = build_target_table_prompt_snapshot(
        {
            "table_fullname": "db.public.orders",
            "available_columns": [
                {
                    "column_fullname": "db.public.orders.order_id",
                    "column_name": "order_id",
                    "is_linked_column": True,
                },
                {
                    "column_fullname": "db.public.orders.created_at",
                    "column_name": "created_at",
                    "is_linked_column": False,
                },
            ],
            "linked_columns": [
                {
                    "column_fullname": "db.public.orders.order_id",
                    "column_name": "order_id",
                }
            ],
            "linked_sample_rows": [{"order_id": 1}],
        }
    )

    assert "available_columns" not in snapshot
    assert "linked_columns" not in snapshot
    assert "linked_sample_rows" not in snapshot
    assert snapshot["columns"] == [
        {
            "column_fullname": "db.public.orders.order_id",
            "column_name": "order_id",
        },
        {
            "column_fullname": "db.public.orders.created_at",
            "column_name": "created_at",
        },
    ]
    assert snapshot["sample_rows"] == [{"order_id": 1}]


def test_run_case_writes_enriched_schema_linking_when_requested() -> None:
    tmp_path = Path("tests/_tmp") / f"table_semantic_{uuid.uuid4().hex}"
    case_dir = tmp_path / "q1_case"
    case_dir.mkdir(parents=True)
    try:
        schema_linking_path = case_dir / "schema_linking.json"
        output_path = case_dir / "linked_table_profile.json"
        schema_linking_path.write_text(
            """
{
  "question": {
    "question_id": "q1",
    "db_id": "db1",
    "question": "show orders",
    "schema_snapshot": {
      "tables": [
        {
          "table_fullname": "db1.public.orders",
          "table_name": "orders",
          "columns": [
            {
              "column_fullname": "db1.public.orders.order_id",
              "column_name": "order_id"
            },
            {
              "column_fullname": "db1.public.orders.customer_id",
              "column_name": "customer_id"
            }
          ],
          "sample_rows": [
            {
              "order_id": 1,
              "customer_id": 10
            }
          ]
        }
      ]
    }
  },
  "Order": {
    "db_id": "db1",
    "question": "show orders",
    "linked_tables": [
      "db1.public.orders"
    ],
    "linked_columns": [
      "db1.public.orders.order_id",
      "db1.public.orders.customer_id"
    ]
  }
}
""".strip(),
            encoding="utf-8",
        )
        (case_dir / "nl2er_output.json").write_text(
            """
{
  "resolve_process": [
    "Subquery 1: Find orders.",
    "Subquery 2: Group orders by customer."
  ]
}
""".strip(),
            encoding="utf-8",
        )
        runner = TableSemanticSketchRunner(
            prompt_dir="src/prompt/prompt_template",
            log_dir=tmp_path / "log",
            dry_run=True,
        )

        payload = runner.run_case(
            case_input=CaseInput(
                case_dir=case_dir,
                relative_case_dir=Path(case_dir.name),
                schema_linking_path=schema_linking_path,
                output_path=output_path,
            ),
            args=argparse.Namespace(
                question=None,
                schema_linking_path=None,
                question_id=None,
                db_id=None,
                db_hint="",
                external_knowledge="",
                nl2er_output_path=None,
                nl2er_output_filename="nl2er_output.json",
                table_fullname=None,
                sample_row_limit=1,
                sample_value_max_chars=100,
                max_table_concurrency=None,
                write_enriched_schema_linking=True,
            ),
        )

        enriched = json.loads(schema_linking_path.read_text(encoding="utf-8"))
        assert payload["ok"] is True
        assert enriched["Order"]["linked_tables"] == ["db1.public.orders"]
        assert payload["sub_questions"] == (
            "Subquery 1: Find orders.\nSubquery 2: Group orders by customer."
        )
        assert enriched["question"]["linked_tables"] == ["db1.public.orders"]
        assert enriched["question"]["sub_questions"] == payload["sub_questions"]
        assert set(enriched["question"]["linked_columns"]) == {
            "db1.public.orders.order_id",
            "db1.public.orders.customer_id",
        }
        assert enriched["question"]["schema_linking"]["source"] == "table_semantic_sketch_output"
        assert len(enriched["linked_table_profiles"]) == 1
        profile = enriched["linked_table_profiles"][0]
        assert profile["table_fullname"] == "db1.public.orders"
        assert profile["is_relevant"] is True
        assert "semantic_units" in profile
        prompt_text = next((tmp_path / "log" / "table_prompts").glob("*.md")).read_text(
            encoding="utf-8"
        )
        assert "Subquery 1: Find orders." in prompt_text
        assert "Subquery 2: Group orders by customer." in prompt_text
        assert "## Analyze Target Table Schema" in prompt_text
        assert "## Columns In This Table" not in prompt_text
        assert '"linked_columns"' not in prompt_text
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)
