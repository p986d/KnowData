import argparse
from dataclasses import replace

from src.config import load_settings
from src.llm.llm_client import LLMClient
from src.llm.reasoning import REASONING_MODE_MAP, apply_reasoning_mode


if __name__ == "__main__":
    settings = load_settings()

    parser = argparse.ArgumentParser()
    parser.add_argument("--model", type=str, default=None)
    parser.add_argument("--prompt", type=str, default=None)
    parser.add_argument("--prompt_file", type=str, default="prompt.md")
    parser.add_argument("--response_file", type=str, default="response.md")
    parser.add_argument(
        "--reasoning-mode",
        choices=sorted(REASONING_MODE_MAP.keys()),
        default=None,
    )
    parser.add_argument(
        "--thinking-type",
        choices=["enabled", "disabled"],
        default=None,
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["high", "max"],
        default=None,
    )
    args = parser.parse_args()

    config = apply_reasoning_mode(settings.llm.get(args.model), args.reasoning_mode)

    thinking_type = config.thinking_type
    reasoning_effort = config.reasoning_effort

    if args.thinking_type is not None:
        thinking_type = args.thinking_type
    if args.reasoning_effort is not None:
        reasoning_effort = args.reasoning_effort
    if thinking_type == "disabled":
        reasoning_effort = None

    config = replace(
        config,
        thinking_type=thinking_type,
        reasoning_effort=reasoning_effort,
    )
    client = LLMClient(config)

    if args.prompt is not None:
        prompt = args.prompt
    else:
        with open(args.prompt_file, "r", encoding="utf-8") as f:
            prompt = f.read()

    response = client.single_turn(prompt)

    
    with open(args.response_file, "w", encoding="utf-8") as f:
        f.write(response)

    print(response)
