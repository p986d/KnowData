from __future__ import annotations

from src.nl2er.nl2er_hypothesis import ERExtractor


def test_resolve_process_from_subproblem_analysis_preserves_question_ambiguity():
    subproblem_analysis = {
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "semantic_operation": "filter",
                "semantic_constraints": ["Customer has at least one order."],
                "ambiguity": ["Count orders or distinct order groups."],
            },
            {
                "id": "SQ2",
                "question": "Return customer names.",
                "ambiguity": [],
            },
        ]
    }

    resolve_process = ERExtractor._resolve_process_from_subproblem_analysis(
        subproblem_analysis
    )

    assert resolve_process == [
        {
            "id": "SQ1",
            "question": "Find customers with orders.",
            "ambiguity": ["Count orders or distinct order groups."],
        },
        {
            "id": "SQ2",
            "question": "Return customer names.",
            "ambiguity": [],
        },
    ]


def test_subproblem_analysis_for_er_extraction_removes_ambiguity():
    subproblem_analysis = {
        "language": "English",
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "semantic_operation": "filter",
                "semantic_constraints": ["Customer has at least one order."],
                "ambiguity": ["Count orders or distinct order groups."],
            }
        ],
    }

    er_input = ERExtractor._subproblem_analysis_for_er_extraction(
        subproblem_analysis
    )

    assert er_input == {
        "language": "English",
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "semantic_operation": "filter",
                "semantic_constraints": ["Customer has at least one order."],
            }
        ],
    }


def test_subproblem_analysis_for_er_extraction_can_include_ambiguity():
    subproblem_analysis = {
        "language": "English",
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "semantic_operation": "filter",
                "semantic_constraints": ["Customer has at least one order."],
                "ambiguity": ["Count orders or distinct order groups."],
            }
        ],
    }

    er_input = ERExtractor._subproblem_analysis_for_er_extraction(
        subproblem_analysis,
        include_question_ambiguity=True,
    )

    assert er_input == subproblem_analysis
