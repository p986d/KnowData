from __future__ import annotations

from src.dataconcept.catalog import (
    build_concept_index,
    normalize_cluster_payload,
    normalize_connection_payload,
)
from src.dataconcept.models import (
    ColumnSnapshotEvidence,
    DataSnapshotEvidence,
    DataUnitGrainJudgment,
    DataUnitShapeGrainAnalysis,
    DataUnitShapeJudgment,
)


def make_snapshot(table_fullname: str, columns: list[str]) -> DataSnapshotEvidence:
    return DataSnapshotEvidence(
        snapshot_id=f"snapshot:{table_fullname}",
        source_kind="physical_table",
        table_fullname=table_fullname,
        namespace="db.public",
        table_name=table_fullname.rsplit(".", 1)[-1],
        snapshot_path="",
        columns=[
            ColumnSnapshotEvidence(
                column_fullname=f"{table_fullname}.{column}",
                column_name=column,
                data_type="STRING",
                description="",
            )
            for column in columns
        ],
    )


def make_entity_analysis(table_fullname: str, name: str, id_column: str) -> DataUnitShapeGrainAnalysis:
    return DataUnitShapeGrainAnalysis(
        snapshot_id=f"snapshot:{table_fullname}",
        table_fullname=table_fullname,
        source_kind="physical_table",
        shape=DataUnitShapeJudgment(shape_type="entity_master", description="Entity table.", confidence="high"),
        grain=DataUnitGrainJudgment(
            grain_kind="entity",
            grain_name=name,
            definition=f"One row is one {name}.",
            identifier_sets=[
                {
                    "identifier_type": "surrogate_key",
                    "columns": [f"{table_fullname}.{id_column}"],
                    "definition": f"{id_column} identifies the {name}.",
                }
            ],
            entity={
                "entity_name": name,
                "definition": f"A {name} concept.",
                "representative_columns": [f"{table_fullname}.{id_column}"],
            },
        ),
    )


def make_relationship_analysis(table_fullname: str) -> DataUnitShapeGrainAnalysis:
    return DataUnitShapeGrainAnalysis(
        snapshot_id=f"snapshot:{table_fullname}",
        table_fullname=table_fullname,
        source_kind="physical_table",
        shape=DataUnitShapeJudgment(shape_type="relationship", description="Relation table.", confidence="high"),
        grain=DataUnitGrainJudgment(
            grain_kind="relationship",
            grain_name="Person Label Assignment",
            definition="One row assigns one label to one person.",
            identifier_sets=[
                {
                    "identifier_type": "natural_key",
                    "columns": [
                        f"{table_fullname}.person_id",
                        f"{table_fullname}.label_id",
                    ],
                    "definition": "The person and label identify the assignment.",
                }
            ],
            relationship={
                "relationship_name": "Person Label Assignment",
                "definition": "A person has a label.",
                "participant_entities": [
                    {
                        "entity_name": "Person",
                        "role": "labeled_subject",
                        "identifier_columns": [f"{table_fullname}.person_id"],
                    },
                    {
                        "entity_name": "Label",
                        "role": "assigned_label",
                        "identifier_columns": [f"{table_fullname}.label_id"],
                    },
                ],
            },
        ),
    )


def test_build_concept_index_from_shape_grain_analyses() -> None:
    analyses = [
        make_entity_analysis("db.public.person", "Person", "person_id"),
        make_relationship_analysis("db.public.person_label"),
    ]

    concept_index = build_concept_index(analyses)

    assert [item["concept_ref"] for item in concept_index] == [
        "unit:db.public.person",
        "unit:db.public.person_label",
    ]
    assert concept_index[0]["concept_name"] == "Person"
    assert concept_index[1]["concept_name"] == "Person Label Assignment"
    assert concept_index[1]["representative_columns"] == [
        "db.public.person_label.person_id",
        "db.public.person_label.label_id",
    ]


def test_normalize_cluster_payload_keeps_clusters_without_merging() -> None:
    concept_index = build_concept_index(
        [
            make_entity_analysis("db.public.person", "Person", "person_id"),
            make_entity_analysis("db.public.resident", "Resident", "resident_id"),
        ]
    )

    clusters = normalize_cluster_payload(
        parsed={
            "concept_clusters": [
                {
                    "cluster_id": "person_like",
                    "cluster_label": "Person-like concepts",
                    "members": ["unit:db.public.person", "unit:db.public.resident"],
                    "cluster_reason": "Both concepts describe people.",
                }
            ]
        },
        concept_index=concept_index,
    )

    assert len(clusters) == 1
    assert clusters[0]["cluster_label"] == "Person-like concepts"
    assert [member["concept_ref"] for member in clusters[0]["members"]] == [
        "unit:db.public.person",
        "unit:db.public.resident",
    ]
    assert clusters[0]["members"][0]["grain_kind"] == "entity"


def test_normalize_connection_payload_resolves_columns_and_target_ref() -> None:
    person_snapshot = make_snapshot("db.public.person", ["person_id", "name"])
    label_snapshot = make_snapshot("db.public.label", ["label_id", "label_name"])
    relation_snapshot = make_snapshot("db.public.person_label", ["person_id", "label_id"])
    person_analysis = make_entity_analysis("db.public.person", "Person", "person_id")
    label_analysis = make_entity_analysis("db.public.label", "Label", "label_id")
    relation_analysis = make_relationship_analysis("db.public.person_label")
    concept_index = build_concept_index([person_analysis, label_analysis, relation_analysis])

    profile = normalize_connection_payload(
        parsed={
            "connections": [
                {
                    "source_columns": ["person_id"],
                    "target_table": "db.public.person_label",
                    "target_identifier_columns": ["person_id"],
                    "connection_type": "hierarchy_reference",
                    "confidence": "low",
                    "definition": "Invalid self-reference through its own identifier.",
                },
                {
                    "source_columns": ["person_id"],
                    "target_table": "db.public.person",
                    "target_identifier_columns": ["person_id"],
                    "connection_type": "participant_reference",
                    "confidence": "high",
                    "definition": "person_id identifies the participating person.",
                }
            ]
        },
        source_snapshot=relation_snapshot,
        source_analysis=relation_analysis,
        concept_index=concept_index,
        snapshots=[person_snapshot, label_snapshot, relation_snapshot],
    )

    assert profile["source_concept_ref"] == "unit:db.public.person_label"
    assert len(profile["connections"]) == 1
    assert profile["connections"][0]["source_columns"] == ["db.public.person_label.person_id"]
    assert profile["connections"][0]["target_concept_ref"] == "unit:db.public.person"
    assert profile["connections"][0]["target_identifier_columns"] == ["db.public.person.person_id"]
