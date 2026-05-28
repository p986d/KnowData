import json
import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

from src.er2data import sql_candidates as er2data_2


def _unit_result(
    *,
    unit_id: str,
    unit_type: str,
    unit_name: str,
    question: str,
) -> dict[str, object]:
    return {
        "unit_id": unit_id,
        "target_unit_type": unit_type,
        "target_unit_name": unit_name,
        "target_unit": {"name": unit_name},
        "question": question,
        "question_generation": {
            "ok": True,
            "error": None,
        },
        "schema_linking": {
            "ok": None,
            "tables": [],
            "columns": [],
        },
    }


class FakeInnerRunner:
    def __init__(self) -> None:
        self.prompt_contexts: list[dict[str, str]] = []

    def build_units(self, _er_model: dict[str, object]) -> list[object]:
        return [
            SimpleNamespace(unit_id="entity::BikeTrip", target_unit_name="BikeTrip"),
            SimpleNamespace(
                unit_id="relationship::TripStation",
                target_unit_name="TripStation",
            ),
            SimpleNamespace(
                unit_id="connection::IgnoredConnection",
                target_unit_name="IgnoredConnection",
            ),
        ]

    def generate_questions(
        self,
        _units: list[object],
        *,
        prompt_context: dict[str, str] | None = None,
        max_concurrency: int | None = None,
    ) -> list[dict[str, object]]:
        self.prompt_contexts.append(dict(prompt_context or {}))
        return [
            _unit_result(
                unit_id="entity::BikeTrip",
                unit_type="entity",
                unit_name="BikeTrip",
                question="Find bike trips.",
            ),
            _unit_result(
                unit_id="relationship::TripStation",
                unit_type="relationship",
                unit_name="TripStation",
                question="Find trip station mappings.",
            ),
            _unit_result(
                unit_id="connection::IgnoredConnection",
                unit_type="connection",
                unit_name="IgnoredConnection",
                question="Find ignored connections.",
            ),
        ]


def test_er2data_2_generates_candidates_for_entities_and_relationships_only(
    monkeypatch,
) -> None:
    tmp_path = Path("tests/_tmp") / f"er2data_2_candidates_{uuid.uuid4().hex}"
    tmp_path.mkdir(parents=True)
    input_path = tmp_path / "nl2er_output.json"
    output_path = tmp_path / "sql_candidates.json"
    try:
        input_path.write_text(
            json.dumps(
                {
                    "entities": [
                        {
                            "entity_name": "BikeTrip",
                            "primary_key": ["trip_id"],
                            "attributes": [],
                        }
                    ],
                    "relations": [],
                    "connections": [],
                    "conditions": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        runner = object.__new__(er2data_2.ER2DataSQLCandidateRunner)
        runner.runner = FakeInnerRunner()
        captured_units: list[str] = []

        def fake_run_sql_candidate_batch(
            unit_results: list[dict[str, object]],
            **_kwargs: object,
        ) -> None:
            captured_units.extend(str(item["target_unit_type"]) for item in unit_results)
            for item in unit_results:
                item["sql_candidate_generation"] = {
                    "ok": True,
                    "sql_candidates": [f"SELECT '{item['target_unit_name']}' AS unit_name;"],
                    "candidate_count": 1,
                    "engine_result_path": "D:/tmp/result.json",
                    "wrapper_log_path": "D:/tmp/wrapper.json",
                    "error": None,
                }
                item["schema_linking"] = {
                    "ok": True,
                    "tables": [{"fullname": "DB.SCHEMA.TABLE"}],
                    "columns": [{"fullname": "DB.SCHEMA.TABLE.id"}],
                }

        runner.run_sql_candidate_batch = fake_run_sql_candidate_batch
        monkeypatch.setattr(
            er2data_2,
            "resolve_engine_runtime",
            lambda **_kwargs: SimpleNamespace(provider_name="fake"),
        )

        result = runner.run_case(
            input_path=input_path,
            output_path=output_path,
            db_id="TEST_DB",
            engine_provider="fake",
            spider2_root=None,
            reforce_root=None,
            engine_script=None,
            nl2sql_engine_script=None,
            max_question_concurrency=None,
            max_candidate_workers=2,
            candidate_timeout_seconds=1.0,
            candidate_temperature=0.7,
            schema_link_temperature=0.0,
            shortlist_trigger=18,
            max_shortlist_tables=24,
            sample_row_limit=2,
            sample_value_max_chars=300,
            similar_tables_hint_limit=12,
            num_votes=4,
            reforce_max_workers=4,
            max_iter=5,
            generation_model=None,
            column_exploration_model=None,
            vote_model=None,
        )

        payload = json.loads(output_path.read_text(encoding="utf-8"))
        assert captured_units == ["entity", "relationship"]
        assert set(payload) == {"BikeTrip", "TripStation"}
        assert payload["BikeTrip"]["sql_candidates"] == [
            "SELECT 'BikeTrip' AS unit_name;"
        ]
        schema_linking_payload = json.loads(
            output_path.with_name("schema_linking.json").read_text(encoding="utf-8")
        )
        assert schema_linking_payload["BikeTrip"]["linked_tables"] == []
        assert schema_linking_payload["BikeTrip"]["linked_columns"] == []
        assert schema_linking_payload["BikeTrip"]["sources"] == {
            "provider_linking": False,
            "sqlglot_parse": False,
        }
        assert result["summary"]["sql_candidate_target_unit_count"] == 2
        assert result["summary"]["skipped_unit_count"] == 1
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_er2data_2_can_disable_er2query_db_hint_without_disabling_nl2sql(
    monkeypatch,
) -> None:
    tmp_path = Path("tests/_tmp") / f"er2data_2_db_hint_{uuid.uuid4().hex}"
    tmp_path.mkdir(parents=True)
    input_path = tmp_path / "nl2er_output.json"
    output_path = tmp_path / "sql_candidates.json"
    db_hint = "Use analytics.trips for trip facts."
    try:
        input_path.write_text(
            json.dumps(
                {
                    "entities": [
                        {
                            "entity_name": "BikeTrip",
                            "primary_key": ["trip_id"],
                            "attributes": [],
                        }
                    ],
                    "relations": [],
                    "connections": [],
                    "conditions": [],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        (tmp_path / "input.json").write_text(
            json.dumps({"db_hint": db_hint}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        runner = object.__new__(er2data_2.ER2DataSQLCandidateRunner)
        fake_inner = FakeInnerRunner()
        runner.runner = fake_inner
        captured: dict[str, object] = {}

        def fake_run_sql_candidate_batch(
            unit_results: list[dict[str, object]],
            **kwargs: object,
        ) -> None:
            captured["base_db_hint"] = kwargs["base_db_hint"]
            for item in unit_results:
                item["sql_candidate_generation"] = {
                    "ok": True,
                    "sql_candidates": [f"SELECT '{item['target_unit_name']}' AS unit_name;"],
                    "candidate_count": 1,
                    "engine_result_path": "D:/tmp/result.json",
                    "wrapper_log_path": "D:/tmp/wrapper.json",
                    "error": None,
                }
                item["schema_linking"] = {
                    "ok": True,
                    "tables": [],
                    "columns": [],
                }

        runner.run_sql_candidate_batch = fake_run_sql_candidate_batch
        monkeypatch.setattr(
            er2data_2,
            "resolve_engine_runtime",
            lambda **_kwargs: SimpleNamespace(provider_name="fake"),
        )

        result = runner.run_case(
            input_path=input_path,
            output_path=output_path,
            db_id="TEST_DB",
            engine_provider="fake",
            spider2_root=None,
            reforce_root=None,
            engine_script=None,
            nl2sql_engine_script=None,
            max_question_concurrency=None,
            max_candidate_workers=2,
            candidate_timeout_seconds=1.0,
            candidate_temperature=0.7,
            schema_link_temperature=0.0,
            shortlist_trigger=18,
            max_shortlist_tables=24,
            sample_row_limit=2,
            sample_value_max_chars=300,
            similar_tables_hint_limit=12,
            num_votes=4,
            reforce_max_workers=4,
            max_iter=5,
            generation_model=None,
            column_exploration_model=None,
            vote_model=None,
            enable_nl2sql_db_hint=True,
            enable_er2query_db_hint=False,
        )

        assert fake_inner.prompt_contexts[0]["db_hint"] == ""
        assert captured["base_db_hint"] == db_hint
        assert result["enable_nl2sql_db_hint"] is True
        assert result["enable_er2query_db_hint"] is False
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_parse_args_accepts_er2data_batch_style_config(monkeypatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "er2data_2",
            "--nl2sql-model-config",
            "deepseek_chat",
            "--include-conditions-in-er2query",
            "true",
            "--enable-nl2sql-db-hint",
            "true",
            "--enable-er2query-db-hint",
            "false",
            "--exclude-desc-in-er2query",
            "false",
            "--max-nl2sql-workers",
            "64",
            "--nl2sql-timeout-seconds",
            "900",
        ],
    )

    args = er2data_2.parse_args()

    assert args.candidate_model_config == "deepseek_chat"
    assert args.include_conditions_in_er2query is True
    assert args.enable_nl2sql_db_hint is True
    assert args.enable_er2query_db_hint is False
    assert args.exclude_desc_in_er2query is False
    assert args.max_candidate_workers == 64
    assert args.candidate_timeout_seconds == 900


def test_schema_linking_output_uses_only_candidate_sql_parse_results() -> None:
    payload = er2data_2.build_schema_linking_output(
        db_id="TEST_DB",
        unit_results=[
            {
                "unit_id": "entity::Trip",
                "target_unit_name": "Trip",
                "question": "Find trips.",
                "question_generation": {"ok": True, "error": None},
                "sql_candidate_generation": {
                    "ok": True,
                    "sql_candidates": ["SELECT id FROM trips"],
                    "error": None,
                },
                "schema_linking": {
                    "ok": True,
                    "tables": [{"fullname": "provider_only_table"}],
                    "columns": [{"fullname": "provider_only_table.provider_only_column"}],
                    "error": None,
                },
            }
        ],
    )

    assert payload["Trip"]["linked_tables"] == ["trips"]
    assert payload["Trip"]["linked_columns"] == ["trips.id"]
    assert payload["Trip"]["sources"] == {
        "provider_linking": False,
        "sqlglot_parse": True,
    }


def test_merge_primary_and_diff_units_merges_same_entity_attributes() -> None:
    primary = SimpleNamespace(
        unit_id="entity::Patent",
        target_unit_type="entity",
        target_unit_name="Patent",
        target_unit={
            "name": "Patent",
            "identifier_attrs": ["patent_id"],
            "attrs": [{"name": "patent_id"}],
        },
        applied_conditions=[],
        condition_selection=[],
    )
    diff = SimpleNamespace(
        unit_id="entity::Patent",
        target_unit_type="entity",
        target_unit_name="Patent",
        target_unit={
            "name": "Patent",
            "identifier_attrs": ["publication_number"],
            "attrs": [{"name": "publication_number"}],
        },
        applied_conditions=[],
        condition_selection=[],
    )

    merged = er2data_2.merge_primary_and_diff_units([primary], [diff])

    assert len(merged) == 1
    assert merged[0].unit_id == "entity::Patent"
    assert merged[0].target_unit_name == "Patent"
    assert merged[0].target_unit["identifier_attrs"] == [
        "patent_id",
        "publication_number",
    ]
    assert [attr["name"] for attr in merged[0].target_unit["attrs"]] == [
        "patent_id",
        "publication_number",
    ]


def test_merge_primary_and_diff_units_merges_same_relationship_by_participant_entities() -> None:
    primary = SimpleNamespace(
        unit_id="relationship::Citation",
        target_unit_type="relationship",
        target_unit_name="Citation",
        target_unit={
            "name": "Citation",
            "participants": [
                {"entity": "Patent", "role": "citing", "identifier_attrs": ["patent_id"]},
                {"entity": "Patent", "role": "cited", "identifier_attrs": ["patent_id"]},
            ],
            "attrs": [{"name": "citation_date"}],
        },
        applied_conditions=[],
        condition_selection=[],
    )
    diff = SimpleNamespace(
        unit_id="relationship::Citation",
        target_unit_type="relationship",
        target_unit_name="Citation",
        target_unit={
            "name": "Citation",
            "participants": [
                {
                    "entity": "Patent",
                    "role": "referencing",
                    "identifier_attrs": ["publication_number"],
                }
            ],
            "attrs": [{"name": "citation_context"}],
        },
        applied_conditions=[],
        condition_selection=[],
    )

    merged = er2data_2.merge_primary_and_diff_units([primary], [diff])

    assert len(merged) == 1
    assert merged[0].unit_id == "relationship::Citation"
    assert merged[0].target_unit_name == "Citation"
    assert [attr["name"] for attr in merged[0].target_unit["attrs"]] == [
        "citation_date",
        "citation_context",
    ]
    assert [
        participant["identifier_attrs"]
        for participant in merged[0].target_unit["participants"]
    ] == [
        ["patent_id", "publication_number"],
        ["patent_id", "publication_number"],
    ]
