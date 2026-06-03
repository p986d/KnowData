from __future__ import annotations

from pathlib import Path
import uuid

from src.nl2er.input import NL2ERInput
from src.nl2er.nl2er_hypothesis import (
    ERExtractor,
    STRICT_SUBPROBLEM_MODELING_NOTE,
)


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
        "logic_branches": [{"branch_id": "top"}],
        "interpretation_and_ambiguity": "Top-level ambiguity analysis.",
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "semantic_operation": "filter",
                "semantic_constraints": ["Customer has at least one order."],
                "interpretation_and_ambiguity": "Do not pass this to ER extraction.",
                "ambiguity": ["Count orders or distinct order groups."],
                "ambiguity_type": "computation_logic",
                "logic_branches": [
                    {
                        "branch_id": "B1",
                        "sub_questions": [
                            {"id": "SQ1.1", "question": "Count orders."}
                        ],
                    }
                ],
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
                "modeling_note": STRICT_SUBPROBLEM_MODELING_NOTE,
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
                "ambiguity_types": ["scope"],
                "logic_branch": {"branch_id": "B1"},
                "interpretation_and_ambiguity": "Do not pass this to ER extraction.",
            }
        ],
    }

    er_input = ERExtractor._subproblem_analysis_for_er_extraction(
        subproblem_analysis,
        include_question_ambiguity=True,
    )

    assert er_input == {
        "language": "English",
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "semantic_operation": "filter",
                "semantic_constraints": ["Customer has at least one order."],
                "ambiguity": ["Count orders or distinct order groups."],
                "ambiguity_types": ["scope"],
                "modeling_note": STRICT_SUBPROBLEM_MODELING_NOTE,
            }
        ],
    }


def test_subproblem_analysis_for_er_extraction_skips_empty_ambiguity_type_note():
    subproblem_analysis = {
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "ambiguity_type": "",
            }
        ],
    }

    er_input = ERExtractor._subproblem_analysis_for_er_extraction(
        subproblem_analysis,
        include_question_ambiguity=True,
    )

    assert er_input == {
        "resolve_process": [
            {
                "id": "SQ1",
                "question": "Find customers with orders.",
                "ambiguity_type": "",
            }
        ],
    }


class FakeLLM:
    def single_turn(self, prompt: str, check_func=None) -> str:
        return '{"entities": [], "relations": []}'


def test_er_extractor_external_knowledge_is_optional():
    work_dir = Path(".codex_test_workspace") / f"er_extract_{uuid.uuid4().hex}"
    work_dir.mkdir(parents=True, exist_ok=True)
    template_path = work_dir / "er_extract.md"
    template_path.write_text(
        "{% if external_knowledge %}EXT: {{ external_knowledge }}{% endif %}\n"
        "{{ subproblem_analysis }}",
        encoding="utf-8",
    )
    input_payload = NL2ERInput(
        question_id="q1",
        user_intent="List customers.",
        db_id="db",
        external_knowledge="Use active customers.",
    )
    subproblem_analysis = {"resolve_process": [{"id": "SQ1", "question": "List."}]}

    extractor = ERExtractor(
        prompt_dir=work_dir,
        log_dir=work_dir / "log_without_external",
        input_payload=input_payload,
        prompt_template_name=template_path.name,
        llm=FakeLLM(),
    )
    extractor.run_prompt(subproblem_analysis)
    prompt_without_external = (
        work_dir / "log_without_external" / "prompt_2.md"
    ).read_text(encoding="utf-8")

    extractor = ERExtractor(
        prompt_dir=work_dir,
        log_dir=work_dir / "log_with_external",
        input_payload=input_payload,
        prompt_template_name=template_path.name,
        include_external_knowledge=True,
        llm=FakeLLM(),
    )
    extractor.run_prompt(subproblem_analysis)
    prompt_with_external = (work_dir / "log_with_external" / "prompt_2.md").read_text(
        encoding="utf-8"
    )

    assert "Use active customers." not in prompt_without_external
    assert "EXT: Use active customers." in prompt_with_external
