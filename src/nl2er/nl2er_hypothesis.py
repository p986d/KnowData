from __future__ import annotations

import argparse
import json
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.prompt.prompt_builder import PromptBuilder
from src.utils.json_util import json_check, json_parse
from src.nl2er.input import (
    DEFAULT_INPUT_PATH,
    DEFAULT_METADATA_ROOT,
    DEFAULT_PROMPT_DIR,
    NL2ERInput,
    read_input_payload,
    serialize_input_payload,
    write_case_metadata,
)
from src.utils.run_log import (
    build_timestamp,
    emit_step_done_log,
    format_elapsed_seconds,
    resolve_output_path,
    resolve_run_dir,
    resolve_run_log_dir,
    write_json,
)


DEFAULT_LOG_ROOT = Path("log/nl2er_hypothesis")
DEFAULT_RESPONSE_FILENAME = "response.md"
DEFAULT_OUTPUT_FILENAME = "nl2er_output.json"
SINGLE_PROMPT_TEMPLATE_NAME = "NL2ER_ERA_sketch_st1_v0.6.md"
QUESTION_RESOLVER_TEMPLATE_NAME = "NL2ER_ERA_question_resolve_st1_v0.6.md"
QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME = "NL2ER_QuestionResolve_Diff_Construct_v0.1.md"
ER_EXTRACTOR_TEMPLATE_NAME = "NL2ER_ERA_er_extract_st2_v0.6.md"
PROMPT_TEMPLATE_NAME = ER_EXTRACTOR_TEMPLATE_NAME


def _normalize_er_extractor_template_name(prompt_template_name: str | None) -> str:
    normalized = str(prompt_template_name or "").strip()
    if not normalized or normalized == SINGLE_PROMPT_TEMPLATE_NAME:
        return ER_EXTRACTOR_TEMPLATE_NAME
    return normalized


class NL2ERSinglePromptRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        prompt_template_name: str = SINGLE_PROMPT_TEMPLATE_NAME,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.reasoning_mode = reasoning_mode
        self.prompt_template_name = str(prompt_template_name).strip()
        if not self.prompt_template_name:
            raise ValueError("`prompt_template_name` must not be empty.")

        settings = load_settings()
        llm_config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
        self.llm = LLMClient(llm_config)
        self.build_prompt = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    def run_prompt(self) -> str:
        self.build_prompt.register_template(
            name="step_1_run_prompt",
            template_name=self.prompt_template_name,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Run the NL2ER hypothesis prompt and keep the raw model output.",
        )
        prompt = self.build_prompt.build_text(
            "step_1_run_prompt",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
            },
        )

        step = 1
        self._write_text_log(f"prompt_{step}.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        response_text = response.strip()
        self._write_text_log(f"response_{step}.md", response_text)

        if not response_text:
            raise RuntimeError(
                "NL2ER hypothesis runner returned an empty or invalid JSON response. "
                "Check model connectivity, credentials, or prompt validity."
            )

        return response_text

    def run(self) -> dict[str, object]:
        started_at = time.time()

        step_started_at = time.time()
        raw_response = self.run_prompt()
        result = json_parse(raw_response)
        emit_step_done_log(
            prefix="NL2ER-HYPO",
            step="run_prompt",
            elapsed_seconds=time.time() - step_started_at,
            response_chars=len(raw_response),
        )

        return {
            "result": result,
            "raw_response": raw_response,
            "elapsed_seconds": time.time() - started_at,
        }


class QuestionResolver:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        prompt_template_name: str = QUESTION_RESOLVER_TEMPLATE_NAME,
        llm: LLMClient | None = None,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.reasoning_mode = reasoning_mode
        self.prompt_template_name = str(prompt_template_name).strip()
        if not self.prompt_template_name:
            raise ValueError("`prompt_template_name` must not be empty.")

        if llm is None:
            settings = load_settings()
            llm_config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
            llm = LLMClient(llm_config)
        self.llm = llm
        self.build_prompt = prompt_builder or PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    def run_prompt(self) -> str:
        self.build_prompt.register_template(
            name="step_1_resolve_question",
            template_name=self.prompt_template_name,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Resolve the NL2ER question into a structured subproblem sequence.",
        )
        prompt = self.build_prompt.build_text(
            "step_1_resolve_question",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
            },
        )

        self._write_text_log("prompt_1.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        response_text = response.strip()
        self._write_text_log("response_1.md", response_text)

        if not response_text:
            raise RuntimeError(
                "Question resolver returned an empty or invalid JSON response. "
                "Check model connectivity, credentials, or prompt validity."
            )

        return response_text

    def resolve(self) -> dict[str, Any]:
        raw_response = self.run_prompt()
        result = json_parse(raw_response)
        write_json(self.log_dir / "subproblem_analysis.json", result)
        return {
            "result": result,
            "raw_response": raw_response,
        }


class QuestionResolverDiff:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        question_resolver_template_name: str = QUESTION_RESOLVER_TEMPLATE_NAME,
        construct_template_name: str = QUESTION_RESOLVER_DIFF_CONSTRUCT_TEMPLATE_NAME,
        llm: LLMClient | None = None,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.reasoning_mode = reasoning_mode
        self.question_resolver_template_name = str(question_resolver_template_name).strip()
        self.construct_template_name = str(construct_template_name).strip()
        if not self.question_resolver_template_name:
            raise ValueError("`question_resolver_template_name` must not be empty.")
        if not self.construct_template_name:
            raise ValueError("`construct_template_name` must not be empty.")

        if llm is None:
            settings = load_settings()
            llm_config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
            llm = LLMClient(llm_config)
        self.llm = llm
        self.build_prompt = prompt_builder or PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )
        self.question_resolver = QuestionResolver(
            prompt_dir=self.prompt_dir,
            log_dir=self.log_dir / "resolve",
            input_payload=self.input_payload,
            model_config=model_config,
            reasoning_mode=self.reasoning_mode,
            prompt_template_name=self.question_resolver_template_name,
            llm=self.llm,
            prompt_builder=self.build_prompt,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    @staticmethod
    def _format_payload(payload: dict[str, Any]) -> str:
        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _question_ambiguity_units(subproblem_analysis: dict[str, Any]) -> list[dict[str, Any]]:
        raw_items = subproblem_analysis.get("resolve_process")
        if not isinstance(raw_items, list):
            return []

        units: list[dict[str, Any]] = []
        for index, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict):
                continue
            question = str(item.get("question") or "").strip()
            ambiguity = item.get("ambiguity")
            if not question and not ambiguity:
                continue
            units.append(
                {
                    "id": str(item.get("id") or f"SQ{index}").strip() or f"SQ{index}",
                    "question": question,
                    "ambiguity": ambiguity if isinstance(ambiguity, list) else [],
                }
            )
        return units

    def run_construct_prompt(
        self,
        *,
        question_ambiguity_units: list[dict[str, Any]],
    ) -> str:
        self.build_prompt.register_template(
            name="step_2_construct_question_resolve_diff",
            template_name=self.construct_template_name,
            required_vars=["question_ambiguity_units"],
            default_vars={},
            description="Construct actionable question-resolve diffs from a resolved subproblem sequence.",
        )
        prompt = self.build_prompt.build_text(
            "step_2_construct_question_resolve_diff",
            vars={
                "question_ambiguity_units": json.dumps(
                    question_ambiguity_units,
                    ensure_ascii=False,
                    indent=2,
                ),
            },
        )
        self._write_text_log("prompt_diff_construct.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        response_text = response.strip()
        self._write_text_log("response_diff_construct.md", response_text)
        if not response_text:
            raise RuntimeError(
                "Question resolver diff construction returned an empty or invalid JSON response. "
                "Check model connectivity, credentials, or prompt validity."
            )
        return response_text

    def construct(
        self,
        *,
        subproblem_analysis: dict[str, Any],
    ) -> dict[str, Any]:
        question_ambiguity_units = self._question_ambiguity_units(subproblem_analysis)
        raw_response = self.run_construct_prompt(
            question_ambiguity_units=question_ambiguity_units,
        )
        result = json_parse(raw_response)
        write_json(self.log_dir / "question_resolve_diff_construct.json", result)
        return {
            "result": result,
            "raw_response": raw_response,
            "question_ambiguity_units": question_ambiguity_units,
        }

    def run(self) -> dict[str, object]:
        started_at = time.time()

        step_started_at = time.time()
        question_payload = self.question_resolver.resolve()
        subproblem_analysis = dict(question_payload["result"])
        raw_subproblem_response = str(question_payload["raw_response"])
        emit_step_done_log(
            prefix="QUESTION-RESOLVE-DIFF",
            step="resolve_question",
            elapsed_seconds=time.time() - step_started_at,
            response_chars=len(raw_subproblem_response),
            subproblem_count=len(subproblem_analysis.get("resolve_process") or []),
        )

        step_started_at = time.time()
        construct_payload = self.construct(
            subproblem_analysis=subproblem_analysis,
        )
        resolve_diff = dict(construct_payload["result"])
        raw_construct_response = str(construct_payload["raw_response"])
        emit_step_done_log(
            prefix="QUESTION-RESOLVE-DIFF",
            step="construct",
            elapsed_seconds=time.time() - step_started_at,
            response_chars=len(raw_construct_response),
            diff_count=len(resolve_diff.get("resolve_diffs") or []),
        )

        return {
            "subproblem_analysis": subproblem_analysis,
            "question_ambiguity_units": construct_payload["question_ambiguity_units"],
            "resolve_diff": resolve_diff,
            "raw_subproblem_response": raw_subproblem_response,
            "raw_construct_response": raw_construct_response,
            "elapsed_seconds": time.time() - started_at,
        }


class ERExtractor:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        prompt_template_name: str = ER_EXTRACTOR_TEMPLATE_NAME,
        include_question_ambiguity: bool = False,
        llm: LLMClient | None = None,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.reasoning_mode = reasoning_mode
        self.prompt_template_name = _normalize_er_extractor_template_name(prompt_template_name)
        self.include_question_ambiguity = bool(include_question_ambiguity)
        if not self.prompt_template_name:
            raise ValueError("`prompt_template_name` must not be empty.")

        if llm is None:
            settings = load_settings()
            llm_config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
            llm = LLMClient(llm_config)
        self.llm = llm
        self.build_prompt = prompt_builder or PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )

    def _write_text_log(self, filename: str, content: str) -> None:
        (self.log_dir / filename).write_text(content, encoding="utf-8")

    @staticmethod
    def _format_subproblem_analysis(subproblem_analysis: dict[str, Any]) -> str:
        return json.dumps(subproblem_analysis, ensure_ascii=False, indent=2)

    @staticmethod
    def _subproblem_analysis_for_er_extraction(
        subproblem_analysis: dict[str, Any],
        *,
        include_question_ambiguity: bool = False,
    ) -> dict[str, Any]:
        sanitized = dict(subproblem_analysis)
        if include_question_ambiguity:
            return sanitized

        raw_items = subproblem_analysis.get("resolve_process")
        if not isinstance(raw_items, list):
            return sanitized

        resolve_process: list[Any] = []
        for item in raw_items:
            if not isinstance(item, dict):
                resolve_process.append(item)
                continue
            cleaned = {
                key: value
                for key, value in item.items()
                if key != "ambiguity"
            }
            resolve_process.append(cleaned)
        sanitized["resolve_process"] = resolve_process
        return sanitized

    @staticmethod
    def _resolve_process_from_subproblem_analysis(
        subproblem_analysis: dict[str, Any],
    ) -> list[dict[str, Any]]:
        raw_items = subproblem_analysis.get("resolve_process")
        if not isinstance(raw_items, list):
            return []

        resolve_process: list[dict[str, Any]] = []
        for index, item in enumerate(raw_items, start=1):
            if isinstance(item, dict):
                question = str(item.get("question") or "").strip()
                ambiguity = item.get("ambiguity")
                payload = {
                    "id": str(item.get("id") or f"SQ{index}").strip() or f"SQ{index}",
                    "question": question,
                    "ambiguity": ambiguity if isinstance(ambiguity, list) else [],
                }
                if question or payload["ambiguity"]:
                    resolve_process.append(payload)
                    continue

                question = str(item.get("question") or "").strip()
                role = str(item.get("role") or "").strip()
                if question and role:
                    text = f"{question}（作用：{role}）"
                else:
                    text = question or role or json.dumps(item, ensure_ascii=False)
            else:
                text = str(item or "").strip()
            if text:
                resolve_process.append(
                    {
                        "id": f"SQ{index}",
                        "question": text,
                        "ambiguity": [],
                    }
                )
            elif isinstance(item, dict):
                resolve_process.append(
                    {
                        "id": f"SQ{index}",
                        "question": f"sub_question_{index}",
                        "ambiguity": [],
                    }
                )
        return resolve_process

    def run_prompt(self, subproblem_analysis: dict[str, Any]) -> str:
        er_subproblem_analysis = self._subproblem_analysis_for_er_extraction(
            subproblem_analysis,
            include_question_ambiguity=self.include_question_ambiguity,
        )
        self.build_prompt.register_template(
            name="step_2_extract_er",
            template_name=self.prompt_template_name,
            required_vars=["user_intent", "subproblem_analysis"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Extract base logical ER units from the resolved subproblem sequence.",
        )
        prompt = self.build_prompt.build_text(
            "step_2_extract_er",
            vars={
                "user_intent": self.input_payload.user_intent,
                "db_hint": self.input_payload.db_hint,
                "external_knowledge": self.input_payload.external_knowledge,
                "subproblem_analysis": self._format_subproblem_analysis(
                    er_subproblem_analysis
                ),
            },
        )

        self._write_text_log("prompt_2.md", prompt)

        response = self.llm.single_turn(prompt, check_func=json_check)
        response_text = response.strip()
        self._write_text_log("response_2.md", response_text)

        if not response_text:
            raise RuntimeError(
                "ER extractor returned an empty or invalid JSON response. "
                "Check model connectivity, credentials, or prompt validity."
            )

        return response_text

    def extract(self, subproblem_analysis: dict[str, Any]) -> dict[str, Any]:
        raw_response = self.run_prompt(subproblem_analysis)
        result = json_parse(raw_response)
        if not result.get("resolve_process"):
            resolve_process = self._resolve_process_from_subproblem_analysis(subproblem_analysis)
            if resolve_process:
                result["resolve_process"] = resolve_process
        write_json(self.log_dir / "er_extraction.json", result)
        return {
            "result": result,
            "raw_response": raw_response,
        }


class NL2ERHypothesisRunner_2:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
        prompt_template_name: str = PROMPT_TEMPLATE_NAME,
        question_resolver_template_name: str = QUESTION_RESOLVER_TEMPLATE_NAME,
        include_question_ambiguity_in_er_extract: bool = False,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.model_config = model_config
        self.reasoning_mode = reasoning_mode
        self.prompt_template_name = _normalize_er_extractor_template_name(prompt_template_name)
        self.question_resolver_template_name = str(question_resolver_template_name).strip()
        self.include_question_ambiguity_in_er_extract = bool(
            include_question_ambiguity_in_er_extract
        )
        if not self.prompt_template_name:
            raise ValueError("`prompt_template_name` must not be empty.")
        if not self.question_resolver_template_name:
            raise ValueError("`question_resolver_template_name` must not be empty.")

        settings = load_settings()
        llm_config = apply_reasoning_mode(settings.llm.get(model_config), reasoning_mode)
        self.llm = LLMClient(llm_config)
        self.build_prompt = PromptBuilder(
            template_dir=self.prompt_dir,
            strict_undefined=False,
        )
        self.question_resolver = QuestionResolver(
            prompt_dir=self.prompt_dir,
            log_dir=self.log_dir,
            input_payload=self.input_payload,
            model_config=self.model_config,
            reasoning_mode=self.reasoning_mode,
            prompt_template_name=self.question_resolver_template_name,
            llm=self.llm,
            prompt_builder=self.build_prompt,
        )
        self.er_extractor = ERExtractor(
            prompt_dir=self.prompt_dir,
            log_dir=self.log_dir,
            input_payload=self.input_payload,
            model_config=self.model_config,
            reasoning_mode=self.reasoning_mode,
            prompt_template_name=self.prompt_template_name,
            include_question_ambiguity=self.include_question_ambiguity_in_er_extract,
            llm=self.llm,
            prompt_builder=self.build_prompt,
        )

    def run(self) -> dict[str, object]:
        started_at = time.time()

        step_started_at = time.time()
        question_payload = self.question_resolver.resolve()
        subproblem_analysis = dict(question_payload["result"])
        raw_subproblem_response = str(question_payload["raw_response"])
        emit_step_done_log(
            prefix="NL2ER-HYPO",
            step="resolve_question",
            elapsed_seconds=time.time() - step_started_at,
            response_chars=len(raw_subproblem_response),
        )

        step_started_at = time.time()
        er_payload = self.er_extractor.extract(subproblem_analysis)
        result = dict(er_payload["result"])
        resolve_process = self.er_extractor._resolve_process_from_subproblem_analysis(
            subproblem_analysis
        )
        if resolve_process:
            result["resolve_process"] = resolve_process
        raw_response = str(er_payload["raw_response"])
        emit_step_done_log(
            prefix="NL2ER-HYPO",
            step="extract_er",
            elapsed_seconds=time.time() - step_started_at,
            response_chars=len(raw_response),
            entity_count=len(result.get("entities") or []),
            relation_count=len(result.get("relations") or []),
        )

        return {
            "subproblem_analysis": subproblem_analysis,
            "raw_subproblem_response": raw_subproblem_response,
            "result": result,
            "raw_response": raw_response,
            "elapsed_seconds": time.time() - started_at,
        }


# Keep the old public name as the default two-stage runner.
NL2ERHypothesisRunner = NL2ERHypothesisRunner_2
NL2ERSubquestionGenerator = QuestionResolver
NL2ERQuestionResolverDiff = QuestionResolverDiff


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run NL2ER hypothesis with separate question resolution and ER extraction prompts."
    )
    parser.add_argument("--input-path", type=Path, default=DEFAULT_INPUT_PATH)
    parser.add_argument("--question-id", default=None)
    parser.add_argument("--db-id", default=None)
    parser.add_argument("--model-config", default=None)
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument("--output-path", type=Path, default=None)
    parser.add_argument("--prompt-dir", type=Path, default=DEFAULT_PROMPT_DIR)
    parser.add_argument("--prompt-template-name", default=PROMPT_TEMPLATE_NAME)
    parser.add_argument(
        "--question-resolver-template-name",
        default=QUESTION_RESOLVER_TEMPLATE_NAME,
    )
    parser.add_argument(
        "--include-question-ambiguity-in-er-extract",
        action="store_true",
        help="Pass question_resolve ambiguity fields into the ER extraction prompt.",
    )
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument("--log-root", type=Path, default=DEFAULT_LOG_ROOT)
    parser.add_argument("--metadata-dir", type=Path, default=None)
    parser.add_argument("--metadata-root", type=Path, default=DEFAULT_METADATA_ROOT)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_payload = read_input_payload(args.input_path, question_id=args.question_id)
    if args.question_id:
        input_payload = replace(input_payload, question_id=str(args.question_id).strip())
    if args.db_id:
        input_payload = replace(input_payload, db_id=str(args.db_id).strip())

    if not input_payload.question_id:
        raise ValueError(
            "`question_id` is required. Provide it in the input JSON or pass --question-id."
        )

    run_timestamp = build_timestamp()
    run_id = f"{input_payload.question_id}_{run_timestamp}"
    log_dir = resolve_run_log_dir(
        run_prefix=input_payload.question_id,
        log_dir=args.log_dir,
        log_root=args.log_root,
        timestamp=run_timestamp,
    )
    metadata_dir = resolve_run_dir(
        run_prefix=input_payload.question_id,
        run_dir=args.metadata_dir,
        run_root=args.metadata_root,
        timestamp=run_timestamp,
    )
    output_path = resolve_output_path(
        output_path=args.output_path,
        run_dir=metadata_dir,
        default_filename=DEFAULT_OUTPUT_FILENAME,
    )

    serialized_input = serialize_input_payload(input_payload)
    write_case_metadata(
        metadata_dir=metadata_dir,
        serialized_input=serialized_input,
        source_input_path=args.input_path,
        question_id=input_payload.question_id,
    )
    write_json(log_dir / "input.json", serialized_input)

    print(f"[NL2ER-HYPO] run_id={run_id}")
    print(f"[NL2ER-HYPO] question_id={input_payload.question_id}")
    print(f"[NL2ER-HYPO] db_id={input_payload.db_id}")
    print(f"[NL2ER-HYPO] question_resolver_template={args.question_resolver_template_name}")
    print(
        "[NL2ER-HYPO] er_extractor_template="
        f"{_normalize_er_extractor_template_name(args.prompt_template_name)}"
    )
    print(
        "[NL2ER-HYPO] include_question_ambiguity_in_er_extract="
        f"{bool(args.include_question_ambiguity_in_er_extract)}"
    )
    if args.reasoning_mode:
        print(f"[NL2ER-HYPO] reasoning_mode={args.reasoning_mode}")

    runner = NL2ERHypothesisRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=input_payload,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
        prompt_template_name=args.prompt_template_name,
        question_resolver_template_name=args.question_resolver_template_name,
        include_question_ambiguity_in_er_extract=(
            args.include_question_ambiguity_in_er_extract
        ),
    )
    payload = runner.run()

    response_path = metadata_dir / DEFAULT_RESPONSE_FILENAME
    response_path.write_text(str(payload["raw_response"]), encoding="utf-8")
    if "raw_subproblem_response" in payload:
        (metadata_dir / "subproblem_response.md").write_text(
            str(payload["raw_subproblem_response"]),
            encoding="utf-8",
        )
    if "subproblem_analysis" in payload:
        write_json(metadata_dir / "subproblem_analysis.json", payload["subproblem_analysis"])
    write_json(output_path, payload["result"])

    print(f"[NL2ER-HYPO] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
    print(f"[NL2ER-HYPO] output wrote to {output_path}")
    print(f"[NL2ER-HYPO] response wrote to {response_path}")
    print(f"[NL2ER-HYPO] logs wrote to {log_dir}")
    print(f"[NL2ER-HYPO] metadata wrote to {metadata_dir}")
    print("[NL2ER-HYPO] response follows")
    print(payload["raw_response"])


if __name__ == "__main__":
    main()
