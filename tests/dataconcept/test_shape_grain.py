from __future__ import annotations

from src.dataconcept.evidence import build_data_snapshot_from_table
from src.dataconcept.shape_grain import ShapeGrainAnalyzer, normalize_shape_grain_analysis
from src.er2data.physical_schema import TableMetadata


def make_snapshot():
    table = TableMetadata(
        full_name="db.public.school_year_stat",
        namespace="db.public",
        short_name="school_year_stat",
        column_names=["school_id", "year", "student_count"],
        column_types=["NUMBER", "NUMBER", "NUMBER"],
        descriptions=["", "", ""],
        sample_rows=[
            {"school_id": 1, "year": 2025, "student_count": 100},
            {"school_id": 1, "year": 2026, "student_count": 110},
        ],
        snapshot_path="databases/db/school_year_stat.json",
        explicit_constraints={
            "primary_keys": ["school_id", "year"],
            "unique_keys": [["school_id", "year"]],
        },
    )
    return build_data_snapshot_from_table(table)


def test_shape_grain_prompt_is_question_independent_and_includes_constraints() -> None:
    analyzer = ShapeGrainAnalyzer(dry_run=True)
    prompt = analyzer.build_prompt(make_snapshot())

    assert "Data Snapshot" in prompt
    assert "Original User Question" not in prompt
    assert "user_intent" not in prompt
    assert '"provided_constraints": {' in prompt
    assert "may be empty or incomplete" in prompt
    assert "explicit primary keys" not in prompt
    assert "fixed suffix" in prompt.lower()
    assert "keyword rules" in prompt.lower()


def test_normalize_shape_grain_analysis_parses_llm_payload() -> None:
    snapshot = make_snapshot()

    analysis = normalize_shape_grain_analysis(
        parsed={
            "data_unit_shape_grain_analysis": {
                "snapshot_id": snapshot.snapshot_id,
                "table_fullname": snapshot.table_fullname,
                "shape": {
                    "shape_type": "metric_fact",
                    "description": "School-year statistic table.",
                    "confidence": "high",
                },
                "grain": {
                    "grain_kind": "entity",
                    "grain_name": "School Year Statistic",
                    "definition": "One row is one school-year statistic entity record.",
                    "identifier_sets": [
                        {
                            "identifier_type": "declared_primary_key",
                            "columns": ["school_id", "year"],
                            "definition": "The declared key identifies one school-year statistic row.",
                        },
                        {
                            "identifier_type": "natural_key",
                            "columns": ["school_id", "year"],
                            "definition": "The school and year naturally identify the statistic.",
                        },
                    ],
                    "entity": {
                        "entity_name": "School Year Statistic",
                        "definition": "A statistic record for one school in one year.",
                        "representative_columns": ["school_id", "year", "student_count"],
                    },
                },
                "analysis_notes": [
                    "Only sampled rows are available."
                ],
            }
        },
        snapshot=snapshot,
    )

    assert analysis.shape.shape_type == "metric_fact"
    assert analysis.grain.grain_kind == "entity"
    assert analysis.grain.grain_name == "School Year Statistic"
    assert analysis.grain.definition == "One row is one school-year statistic entity record."
    assert analysis.grain.identifier_sets[0]["identifier_type"] == "declared_primary_key"
    assert analysis.grain.identifier_sets[1]["identifier_type"] == "natural_key"
    assert analysis.grain.identifier_sets[0]["columns"] == [
        "db.public.school_year_stat.school_id",
        "db.public.school_year_stat.year",
    ]
    assert analysis.grain.entity["entity_name"] == "School Year Statistic"
    assert analysis.grain.entity["representative_columns"] == [
        "db.public.school_year_stat.school_id",
        "db.public.school_year_stat.year",
        "db.public.school_year_stat.student_count",
    ]
    assert analysis.analysis_notes == ["Only sampled rows are available."]
    assert analysis.error == ""


def test_normalize_shape_grain_analysis_parses_relationship_grain() -> None:
    snapshot = make_snapshot()

    analysis = normalize_shape_grain_analysis(
        parsed={
            "shape_grain_analysis": {
                "shape": {
                    "shape_type": "relationship_bridge",
                    "description": "A relation table.",
                    "confidence": "medium",
                },
                "grain": {
                    "grain_kind": "relationship",
                    "grain_name": "School Year Measurement",
                    "definition": "One row relates a school to a reporting year.",
                    "identifier_sets": [
                        {
                            "identifier_type": "composite_key",
                            "columns": ["school_id", "year"],
                            "definition": "The participant and year identify the row.",
                        }
                    ],
                    "relationship": {
                        "relationship_name": "School Year Measurement",
                        "definition": "A reporting-year measurement relationship for a school.",
                        "participant_entities": [
                            {
                                "entity_name": "School",
                                "role": "measured_object",
                                "identifier_columns": ["school_id"],
                            }
                        ],
                    },
                },
            }
        },
        snapshot=snapshot,
    )

    assert analysis.shape.shape_type == "relationship"
    assert analysis.grain.grain_kind == "relationship"
    assert analysis.grain.relationship["relationship_name"] == "School Year Measurement"
    assert analysis.grain.identifier_sets[0]["columns"] == [
        "db.public.school_year_stat.school_id",
        "db.public.school_year_stat.year",
    ]
    assert analysis.grain.relationship["participant_entities"][0]["identifier_columns"] == [
        "db.public.school_year_stat.school_id"
    ]


def test_shape_grain_dry_run_returns_unknown_without_rule_judgment() -> None:
    analyzer = ShapeGrainAnalyzer(dry_run=True)

    analysis = analyzer.analyze_snapshot(make_snapshot())

    assert analysis.shape.shape_type == "unknown"
    assert analysis.grain.grain_kind == "unknown"
    assert analysis.grain.definition.startswith("Dry-run placeholder")
    assert analysis.analysis_notes
    assert "Dry-run placeholder" in analysis.shape.description
