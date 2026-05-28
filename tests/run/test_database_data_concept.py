from __future__ import annotations

import uuid
from pathlib import Path

from src.run.database_data_concept import DatabaseDataConceptRunner


def test_database_data_concept_runner_dry_run_writes_output() -> None:
    tmp_root = Path("tests/_tmp") / f"database_data_concept_{uuid.uuid4().hex}"
    output_path = tmp_root / "output.json"
    runner = DatabaseDataConceptRunner(
        db_id="sy_community_link",
        database_root="databases",
        spider2_root=None,
        log_dir=tmp_root / "log",
        prompt_dir="src/prompt/prompt_template",
        dry_run=True,
    )

    payload = runner.run(
        output_path=output_path,
        table_filters={"sy_community_link.ads_person_basic_info".casefold()},
        max_concurrency=1,
    )

    assert payload["ok"] is True
    assert payload["snapshot_count"] == 1
    assert payload["analysis_count"] == 1
    assert "data_unit_concept_profiles" not in payload
    assert "database_concept_catalog" in payload
    assert len(payload["database_concept_catalog"]["concept_index"]) == 1
    assert payload["database_concept_catalog"]["concept_clusters"] == []
    assert len(payload["database_concept_catalog"]["source_connection_profiles"]) == 1
    assert payload["shape_grain_analyses"][0]["shape"]["shape_type"] == "unknown"
    assert output_path.exists()
    assert (tmp_root / "log" / "shape_grain_prompts").exists()
    assert (tmp_root / "log" / "catalog_prompts" / "concept_clustering.md").exists()
