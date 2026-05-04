from __future__ import annotations

import argparse
import time
from dataclasses import replace
from pathlib import Path

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode
from src.prompt.prompt_builder import PromptBuilder
from src.run.nl2er_2 import (
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


DEFAULT_LOG_ROOT = Path("log/nl2er_3")
DEFAULT_OUTPUT_FILENAME = "response.md"
PROMPT_TEMPLATE_NAME = "NL2ER_ER_sketch_st1_v0.4.md"


class NL2ERSinglePromptRunner:
    def __init__(
        self,
        *,
        prompt_dir: str | Path,
        log_dir: str | Path,
        input_payload: NL2ERInput,
        model_config: str | None = None,
        reasoning_mode: str | None = None,
    ) -> None:
        self.prompt_dir = Path(prompt_dir)
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.input_payload = input_payload
        self.reasoning_mode = reasoning_mode

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
            template_name=PROMPT_TEMPLATE_NAME,
            required_vars=["user_intent"],
            default_vars={"db_hint": "", "external_knowledge": ""},
            description="Run the NL2ER ER test stage1 prompt and keep the raw model output.",
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

        response = self.llm.single_turn(prompt)
        response_text = response.strip()
        self._write_text_log(f"response_{step}.md", response_text)

        if not response_text:
            raise RuntimeError(
                "NL2ER-3 prompt runner returned an empty response. "
                "Check model connectivity, credentials, or prompt validity."
            )

        return response_text

    def run(self) -> dict[str, object]:
        started_at = time.time()

        step_started_at = time.time()
        result = self.run_prompt()
        emit_step_done_log(
            prefix="NL2ER-3",
            step="run_prompt",
            elapsed_seconds=time.time() - step_started_at,
            response_chars=len(result),
        )

        return {
            "result": result,
            "elapsed_seconds": time.time() - started_at,
        }


# Keep the old export name available for local callers.
NL2ERSubquestionGenerator = NL2ERSinglePromptRunner


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run NL2ER-3 as a single prompt call from a parameterized JSON input."
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

    print(f"[NL2ER-3] run_id={run_id}")
    print(f"[NL2ER-3] question_id={input_payload.question_id}")
    print(f"[NL2ER-3] db_id={input_payload.db_id}")
    print(f"[NL2ER-3] prompt_template={PROMPT_TEMPLATE_NAME}")
    if args.reasoning_mode:
        print(f"[NL2ER-3] reasoning_mode={args.reasoning_mode}")

    runner = NL2ERSinglePromptRunner(
        prompt_dir=args.prompt_dir,
        log_dir=log_dir,
        input_payload=input_payload,
        model_config=args.model_config,
        reasoning_mode=args.reasoning_mode,
    )
    payload = runner.run()

    output_path.write_text(str(payload["result"]), encoding="utf-8")

    print(f"[NL2ER-3] elapsed={format_elapsed_seconds(payload['elapsed_seconds'])}")
    print(f"[NL2ER-3] output wrote to {output_path}")
    print(f"[NL2ER-3] logs wrote to {log_dir}")
    print(f"[NL2ER-3] metadata wrote to {metadata_dir}")
    print("[NL2ER-3] response follows")
    print(payload["result"])


if __name__ == "__main__":
    main()
