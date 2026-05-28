from __future__ import annotations

import json
from pathlib import Path
import uuid

from src.run import knowdata_question_resolver_diff_stage


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_knowdata_question_resolver_diff_stage_writes_expected_outputs(monkeypatch):
    work_dir = Path(".codex_test_workspace") / f"question_resolver_diff_{uuid.uuid4().hex}"
    input_path = work_dir / "input.json"
    metadata_root = work_dir / "metadata"
    log_root = work_dir / "log"
    write_json(
        input_path,
        [
            {
                "question_id": "case_001",
                "user_intent": "List customers with at least one order.",
                "db_id": "local_db",
            }
        ],
    )

    class FakeQuestionResolverDiff:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def run(self):
            return {
                "subproblem_analysis": {
                    "domain": "business",
                    "language": "English",
                    "resolve_process": [
                        {
                            "id": "SQ1",
                            "question": "Find customers with orders.",
                            "semantic_operation": "filter",
                            "semantic_constraints": [],
                            "ambiguity": [],
                        }
                    ],
                },
                "resolve_diff": {
                    "ok": True,
                    "question_patches": [
                        {
                            "id": "QP1",
                            "target_id": "SQ1",
                            "question": "Find customers with at least one distinct order.",
                        }
                    ],
                },
                "elapsed_seconds": 0.1,
            }

    monkeypatch.setattr(
        knowdata_question_resolver_diff_stage,
        "QuestionResolverDiff",
        FakeQuestionResolverDiff,
    )

    parser = knowdata_question_resolver_diff_stage.build_arg_parser()
    args = parser.parse_args(
        [
            "--input-path",
            str(input_path),
            "--metadata-root",
            str(metadata_root),
            "--log-root",
            str(log_root),
            "--timestamp",
            "stage_test",
        ]
    )

    summary = knowdata_question_resolver_diff_stage.run_question_resolver_diff_stage(args)

    assert summary["status"] == "succeeded"
    case_dir = metadata_root / "stage_test" / "case_001_stage_test"
    assert {path.name for path in case_dir.iterdir()} == {
        "subproblem_analysis.json",
        "question_resolver_diff.json",
    }
    assert (
        log_root
        / "stage_test"
        / "knowdata_question_resolver_diff_stage_summary.json"
    ).exists()
    diff_payload = json.loads(
        (case_dir / "question_resolver_diff.json").read_text(encoding="utf-8")
    )
    assert diff_payload["summary"] == {
        "subproblem_count": 1,
        "question_patch_count": 1,
    }
