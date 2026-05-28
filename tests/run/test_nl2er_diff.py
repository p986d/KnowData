import json
import shutil
import sys
import uuid
from pathlib import Path

from src.nl2er import nl2er_hypothesis_diff as nl2er_diff
from src.nl2er.nl2er_hypothesis_diff import (
    DEFAULT_BATCH_SUMMARY_FILENAME,
    DEFAULT_OUTPUT_FILENAME,
    NL2ERDiffConstructor,
    build_nl2er_output_diff_payload,
    build_semantic_unit_inventory,
    discover_case_inputs,
    normalize_er_core,
    validate_diff_ideas_payload,
)


def sample_er() -> dict:
    return {
        "entities": [
            {
                "entity_name": "Order",
                "desc": "A customer order.",
                "grain": "One order",
                "attributes": [
                    {"name": "order_id", "semantics": "Order identifier"},
                    {"name": "customer_id", "semantics": "Customer identifier"},
                    {"name": "amount", "semantics": "Order amount"},
                ],
                "primary_key": ["order_id"],
            },
            {
                "entity_name": "Customer",
                "desc": "A customer.",
                "grain": "One customer",
                "attributes": [
                    {"name": "customer_id", "semantics": "Customer identifier"},
                    {"name": "name", "semantics": "Customer name"},
                ],
                "primary_key": ["customer_id"],
            },
        ],
        "relations": [
            {
                "relation_name": "Order Customer Mapping",
                "desc": "Maps orders to customers.",
                "grain": "One order customer mapping",
                "participants": [
                    {
                        "role": "order",
                        "entity": "Order",
                        "anchor_attribute": ["order_id"],
                    },
                    {
                        "role": "customer",
                        "entity": "Customer",
                        "anchor_attribute": ["customer_id"],
                    },
                ],
                "link_condition": "Order.customer_id equals Customer.customer_id.",
                "attributes": [],
            }
        ],
        "conditions": [
            {
                "condition_name": "Positive Amount",
                "condition_type": "single_attribute",
                "targets": ["Order.amount"],
                "description": "Only positive orders are included.",
            }
        ],
        "resolve_process": [
            "Find positive orders and relate them to customers."
        ],
    }


def test_validate_diff_ideas_payload_rejects_missing_description() -> None:
    source_er = normalize_er_core(sample_er())
    semantic_units = build_semantic_unit_inventory(source_er)
    ideas, errors, warnings = validate_diff_ideas_payload(
        {
            "diff_ideas": [
                {"description": "not a string"}
            ]
        },
        semantic_units=semantic_units,
        requested_idea_count=1,
    )

    assert ideas == []
    assert warnings == []
    assert any("natural-language string" in error for error in errors)


def test_build_nl2er_output_diff_payload_extracts_diff_semantic_units() -> None:
    payload = {
        "diff_ideas": ["Use a stored order snapshot."],
        "er_model_diff": [
            {
                "diff_idea": "Use a stored order snapshot.",
                "diff_semantic_unit": [
                    {
                        "entity_name": "Order Snapshot",
                        "desc": "Stored order state.",
                        "attributes": {"snapshot_id": "Snapshot identifier"},
                        "primary_key": ["snapshot_id"],
                    },
                    {
                        "relation_name": "Order Snapshot Mapping",
                        "participants": [
                            {
                                "role": "order",
                                "entity": "Order",
                                "anchor_attribute": ["order_id"],
                            }
                        ],
                    },
                ],
            }
        ],
    }

    output = build_nl2er_output_diff_payload(payload)

    assert list(output) == ["entities", "relations", "conditions"]
    assert output["entities"] == [
        {
            "entity_name": "Order Snapshot",
            "desc": "Stored order state.",
            "grain": "",
            "attributes": [
                {
                    "name": "snapshot_id",
                    "semantics": "Snapshot identifier",
                }
            ],
            "primary_key": ["snapshot_id"],
        }
    ]
    assert output["relations"] == [
        {
            "relation_name": "Order Snapshot Mapping",
            "desc": "",
            "grain": "",
            "participants": [
                {
                    "role": "order",
                    "entity": "Order",
                    "anchor_attribute": ["order_id"],
                }
            ],
            "link_condition": "",
            "attributes": [],
        }
    ]
    assert output["conditions"] == []


def test_build_nl2er_output_diff_payload_merges_same_entity_attribute_sets() -> None:
    payload = {
        "er_model_diff": [
            {
                "diff_semantic_unit": [
                    {
                        "entity_name": "Patent",
                        "desc": "Patent record.",
                        "attributes": [{"name": "patent_id", "semantics": "Patent id"}],
                        "primary_key": ["patent_id"],
                    },
                    {
                        "entity_name": "Patent",
                        "grain": "One patent.",
                        "attributes": [{"name": "title", "semantics": "Patent title"}],
                    },
                ]
            }
        ]
    }

    output = build_nl2er_output_diff_payload(payload)

    assert output["entities"] == [
        {
            "entity_name": "Patent",
            "desc": "Patent record.",
            "grain": "One patent.",
            "attributes": [
                {"name": "patent_id", "semantics": "Patent id"},
                {"name": "title", "semantics": "Patent title"},
            ],
            "primary_key": ["patent_id"],
        }
    ]


def test_build_nl2er_output_diff_payload_merges_same_relation_by_name_and_entities() -> None:
    payload = {
        "er_model_diff": [
            {
                "diff_semantic_unit": [
                    {
                        "relation_name": "Patent Citation",
                        "participants": [
                            {"role": "citing", "entity": "Patent"},
                            {"role": "cited", "entity": "Patent"},
                        ],
                        "attributes": [{"name": "citation_date"}],
                    },
                    {
                        "relation_name": "Patent Citation",
                        "participants": [
                            {"role": "source", "entity": "Patent"},
                            {"role": "target", "entity": "Patent"},
                        ],
                        "attributes": [{"name": "citation_count"}],
                    },
                    {
                        "relation_name": "Patent Citation",
                        "participants": [
                            {"role": "patent", "entity": "Patent"},
                            {"role": "category", "entity": "CPC Category"},
                        ],
                        "attributes": [{"name": "assignment_date"}],
                    },
                ]
            }
        ]
    }

    output = build_nl2er_output_diff_payload(payload)

    assert len(output["relations"]) == 2
    assert output["relations"][0]["relation_name"] == "Patent Citation"
    assert output["relations"][0]["attributes"] == [
        {"name": "citation_date", "semantics": ""},
        {"name": "citation_count", "semantics": ""},
    ]
    assert output["relations"][1]["attributes"] == [
        {"name": "assignment_date", "semantics": ""},
    ]


def test_dry_run_constructor_writes_output_and_prompt() -> None:
    tmp_dir = Path("tests/_tmp") / f"nl2er_diff_{uuid.uuid4().hex}"
    case_dir = tmp_dir / "q1_case"
    case_dir.mkdir(parents=True)
    try:
        input_path = case_dir / "nl2er_output.json"
        output_path = case_dir / DEFAULT_OUTPUT_FILENAME
        input_path.write_text(json.dumps(sample_er(), ensure_ascii=False, indent=2), encoding="utf-8")
        (case_dir / "nl2er_input.json").write_text(
            json.dumps(
                {
                    "question_id": "q1",
                    "db_id": "db1",
                    "user_intent": "Show positive order amounts by customer.",
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (case_dir / "schema_linking.json").write_text(
            json.dumps(
                {
                    "question": {
                        "linked_tables": ["db.public.orders"],
                        "linked_columns": ["db.public.orders.order_id"],
                    }
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        runner = NL2ERDiffConstructor(
            prompt_dir="src/prompt/prompt_template",
            log_dir=tmp_dir / "log",
            reasoning_mode="non_think",
            dry_run=True,
        )
        payload = runner.run_case(
            input_path=input_path,
            output_path=output_path,
            idea_count=1,
            include_resolve_process=True,
            include_schema_linking=True,
        )

        assert payload["ok"] is True
        assert payload["status"] == "dry_run"
        assert payload["reasoning_mode"] == "non_think"
        assert payload["question_id"] == "q1"
        assert len(payload["diff_ideas"]) == 1
        assert isinstance(payload["diff_ideas"][0], str)
        assert payload["diff_ideas"][0]
        assert output_path.exists()
        assert (tmp_dir / "log" / "prompt.md").exists()
        assert (tmp_dir / "log" / "response.md").exists()
        assert sorted(path.name for path in (tmp_dir / "log").iterdir()) == [
            "prompt.md",
            "response.md",
        ]
        prompt_text = (tmp_dir / "log" / "prompt.md").read_text(encoding="utf-8")
        assert "Source ER Semantic Units" in prompt_text
        assert "Resolve Process From NL2ER Output" in prompt_text
        assert "Schema Linking" in prompt_text
        output_payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert list(output_payload) == ["entities", "relations", "conditions"]
        assert output_payload["entities"] == []
        assert len(output_payload["relations"]) == 1
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_discover_case_inputs_finds_batch_cases() -> None:
    tmp_dir = Path("tests/_tmp") / f"nl2er_diff_batch_discover_{uuid.uuid4().hex}"
    batch_dir = tmp_dir / "metadata" / "20260507-120109"
    try:
        for case_name in ("q1_20260507-120109", "q2_20260507-120109"):
            case_dir = batch_dir / case_name
            case_dir.mkdir(parents=True)
            (case_dir / "nl2er_output.json").write_text(
                json.dumps(sample_er(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

        batch_root, cases = discover_case_inputs(batch_dir)

        assert batch_root == batch_dir.resolve()
        assert [case.relative_case_dir.as_posix() for case in cases] == [
            "q1_20260507-120109",
            "q2_20260507-120109",
        ]
        assert all(case.input_path.name == "nl2er_output.json" for case in cases)
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_main_dry_run_batch_writes_each_output_and_summary(monkeypatch) -> None:
    tmp_dir = Path("tests/_tmp") / f"nl2er_diff_batch_{uuid.uuid4().hex}"
    batch_dir = tmp_dir / "metadata" / "20260507-120109"
    log_root = tmp_dir / "log"
    try:
        case_names = ("q1_20260507-120109", "q2_20260507-120109")
        for index, case_name in enumerate(case_names, start=1):
            case_dir = batch_dir / case_name
            case_dir.mkdir(parents=True)
            (case_dir / "nl2er_output.json").write_text(
                json.dumps(sample_er(), ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (case_dir / "input.json").write_text(
                json.dumps(
                    {
                        "question_id": f"q{index}",
                        "db_id": "db1",
                        "question": f"Question {index}",
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "run_nl2er_diff.py",
                str(batch_dir),
                "--dry-run",
                "--idea-count",
                "1",
                "--max-batch-workers",
                "2",
                "--log-root",
                str(log_root),
                "--run-name",
                "batch_test",
                "--no-print-ideas",
            ],
        )

        exit_code = nl2er_diff.main()

        assert exit_code == 0
        for case_name in case_names:
            output_path = batch_dir / case_name / DEFAULT_OUTPUT_FILENAME
            assert output_path.exists()
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            assert list(payload) == ["entities", "relations", "conditions"]
            assert len(payload["relations"]) == 1

        summary_path = batch_dir / DEFAULT_BATCH_SUMMARY_FILENAME
        assert summary_path.exists()
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        assert summary["case_count"] == 2
        assert summary["ok_cases"] == 2
        assert summary["failed_cases"] == 0
        assert (log_root / "batch_test" / "batch_summary.json").exists()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
