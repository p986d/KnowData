from __future__ import annotations

import json
from pathlib import Path
import uuid

from src.run import knowdata_pipeline


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_knowdata_pipeline_writes_only_primary_metadata_outputs(monkeypatch):
    work_dir = Path(".codex_test_workspace") / f"knowdata_pipeline_{uuid.uuid4().hex}"
    input_path = work_dir / "input.json"
    metadata_root = work_dir / "metadata"
    log_root = work_dir / "log"
    write_json(
        input_path,
        [
            {
                "question_id": "case_001",
                "user_intent": "List customers.",
                "db_id": "local_db",
                "db_hint": "customer table exists",
                "external_knowledge": "Use active customers.",
            }
        ],
    )

    class FakeNL2ERHypothesisRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            return {
                "result": {
                    "entities": [
                        {
                            "entity_name": "Customer",
                            "primary_key": ["customer_id"],
                            "attributes": [{"name": "customer_id"}],
                        }
                    ],
                    "relations": [],
                    "conditions": [],
                },
                "raw_response": "{}",
                "elapsed_seconds": 0.1,
            }

    class FakeNL2ERHypothesisDiffConstructor:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run_case(self, *, output_path, **kwargs):
            payload = {
                "entities": [
                    {
                        "entity_name": "CustomerSegment",
                        "primary_key": ["segment_id"],
                        "attributes": [{"name": "segment_id"}],
                    }
                ],
                "relations": [
                    {
                        "relation_name": "CustomerSegmentMembership",
                        "participants": [
                            {"entity": "Customer", "role": "customer"},
                            {"entity": "CustomerSegment", "role": "segment"},
                        ],
                        "attributes": [{"name": "membership_status"}],
                    }
                ],
                "conditions": [],
            }
            write_json(Path(output_path), payload)
            return {
                "ok": True,
                "status": "ok",
                "diff_ideas": ["add nothing"],
                "errors": [],
                "warnings": [],
            }

    class FakeER2DataSQLCandidateRunner:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            assert kwargs["log_layout"] == "compact"
            assert kwargs["write_wrapper_logs"] is False
            assert kwargs["create_schema_linking_log_dirs"] is False

        def run_case(self, *, output_path, **kwargs):
            output_path = Path(output_path)
            schema_linking_path = output_path.with_name("schema_linking.json")
            write_json(
                output_path,
                {
                    "Customer": {
                        "db_id": kwargs["db_id"],
                        "unit_type": "entity",
                        "question": "List customers.",
                        "sql_candidates": ["select customer_id from customer"],
                        "candidate_count": 1,
                    }
                },
            )
            write_json(
                schema_linking_path,
                {
                    "Customer": {
                        "db_id": kwargs["db_id"],
                        "question": "List customers.",
                        "linked_tables": ["customer"],
                        "linked_columns": ["customer.customer_id"],
                        "sources": {"provider_linking": False, "sqlglot_parse": True},
                        "candidates": [],
                    }
                },
            )
            return {
                "ok": True,
                "summary": {
                    "total_unit_count": 1,
                    "candidate_success_count": 1,
                },
            }

    monkeypatch.setattr(
        knowdata_pipeline,
        "NL2ERHypothesisRunner",
        FakeNL2ERHypothesisRunner,
    )
    monkeypatch.setattr(
        knowdata_pipeline,
        "NL2ERHypothesisDiffConstructor",
        FakeNL2ERHypothesisDiffConstructor,
    )
    monkeypatch.setattr(
        knowdata_pipeline,
        "ER2DataSQLCandidateRunner",
        FakeER2DataSQLCandidateRunner,
    )
    monkeypatch.setattr(
        knowdata_pipeline,
        "build_schema_snapshot",
        lambda **kwargs: {
            "db_id": kwargs["db_id"],
            "table_count": 1,
            "tables": [{"table_name": "customer", "columns": [{"name": "customer_id"}]}],
        },
    )
    monkeypatch.setattr(
        knowdata_pipeline,
        "select_entities_with_llm",
        lambda **kwargs: {
            "ok": True,
            "error": "",
            "prompt": "prompt",
            "raw_response": "{}",
            "model_config": kwargs["model_config_name"] or "fake",
            "selection": {
                "entities": [{"name": "Customer", "attributes": [{"name": "customer_id"}]}],
                "selected_entities": [
                    {
                        "entity_name": "Customer",
                        "selected_attributes": ["customer_id"],
                    }
                ],
                "relations": [
                    {
                        "name": kwargs["relation_candidates"][0]["name"],
                        "participants": kwargs["relation_candidates"][0]["participants"],
                    }
                ]
                if kwargs["include_relation_candidates"] and kwargs["relation_candidates"]
                else [],
                "selected_relations": [
                    {"relation_name": kwargs["relation_candidates"][0]["name"]}
                ]
                if kwargs["include_relation_candidates"] and kwargs["relation_candidates"]
                else [],
            },
        },
    )

    parser = knowdata_pipeline.build_arg_parser()
    args = parser.parse_args(
        [
            "--input-path",
            str(input_path),
            "--metadata-root",
            str(metadata_root),
            "--log-root",
            str(log_root),
            "--timestamp",
            "batch_test",
        ]
    )

    summary = knowdata_pipeline.run_pipeline(args)

    assert summary["status"] == "succeeded"
    case_dir = metadata_root / "batch_test" / "case_001_batch_test"
    assert {path.name for path in case_dir.iterdir()} == {
        "nl2er.json",
        "nl2er_diff.json",
        "sql_candidates.json",
        "schema_linking.json",
        "er_selection.json",
    }
    assert (log_root / "batch_test" / "knowdata_pipeline_summary.json").exists()
    selection_payload = json.loads((case_dir / "er_selection.json").read_text(encoding="utf-8"))
    assert selection_payload["summary"]["entity_candidate_count"] == 2
    assert selection_payload["summary"]["selected_entity_count"] == 1
    assert selection_payload["summary"]["relation_candidate_count"] == 1
    assert selection_payload["summary"]["selected_relation_count"] == 1
    assert "validation" not in summary
