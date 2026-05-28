from __future__ import annotations

import json
from pathlib import Path
import uuid

from src.run import knowdata_nl2er_stage


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_knowdata_nl2er_stage_writes_nl2er_and_diff_metadata(monkeypatch):
    work_dir = Path(".codex_test_workspace") / f"knowdata_nl2er_stage_{uuid.uuid4().hex}"
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
            }
        ],
    )

    def fake_run_nl2er_case(case, *, args, log_root):
        write_json(
            case.nl2er_path,
            {
                "question_id": case.input_payload.question_id,
                "db_id": case.input_payload.db_id,
                "entities": [{"entity_name": "Customer"}],
                "relations": [],
                "conditions": [],
            },
        )
        return {
            "ok": True,
            "question_id": case.input_payload.question_id,
            "case_dir": str(case.case_dir),
            "output_path": str(case.nl2er_path),
            "log_dir": str(log_root / case.relative_case_dir / "nl2er"),
            "elapsed_seconds": 0.001,
        }

    def fake_run_nl2er_diff_case(case, *, args, log_root):
        write_json(
            case.nl2er_diff_path,
            {
                "question_id": case.input_payload.question_id,
                "db_id": case.input_payload.db_id,
                "entities": [],
                "relations": [],
                "conditions": [],
            },
        )
        return {
            "ok": True,
            "question_id": case.input_payload.question_id,
            "case_dir": str(case.case_dir),
            "input_path": str(case.nl2er_path),
            "output_path": str(case.nl2er_diff_path),
            "log_dir": str(log_root / case.relative_case_dir / "nl2er_diff"),
            "elapsed_seconds": 0.001,
        }

    monkeypatch.setattr(knowdata_nl2er_stage, "run_nl2er_case", fake_run_nl2er_case)
    monkeypatch.setattr(
        knowdata_nl2er_stage,
        "run_nl2er_diff_case",
        fake_run_nl2er_diff_case,
    )

    parser = knowdata_nl2er_stage.build_arg_parser()
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

    summary = knowdata_nl2er_stage.run_nl2er_stage(args)

    assert summary["status"] == "succeeded"
    case_dir = metadata_root / "stage_test" / "case_001_stage_test"
    assert {path.name for path in case_dir.iterdir()} == {"nl2er.json", "nl2er_diff.json"}
    assert summary["outputs"] == {
        "nl2er": "nl2er.json",
        "nl2er_diff": "nl2er_diff.json",
    }
    assert summary["stages"]["nl2er"]["ok_cases"] == 1
    assert summary["stages"]["nl2er_diff"]["ok_cases"] == 1
    assert (log_root / "stage_test" / "knowdata_nl2er_stage_summary.json").exists()
