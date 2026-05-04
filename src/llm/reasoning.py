from __future__ import annotations

from dataclasses import replace

from src.config import LLMConfig


REASONING_MODE_MAP: dict[str, tuple[str, str | None]] = {
    "non_think": ("disabled", None),
    "think_high": ("enabled", "high"),
    "think_max": ("enabled", "max"),
}


def apply_reasoning_mode(config: LLMConfig, reasoning_mode: str | None) -> LLMConfig:
    if reasoning_mode is None:
        return config

    if reasoning_mode not in REASONING_MODE_MAP:
        valid_modes = ", ".join(sorted(REASONING_MODE_MAP))
        raise ValueError(
            f"Unsupported reasoning mode `{reasoning_mode}`. Expected one of: {valid_modes}."
        )

    thinking_type, reasoning_effort = REASONING_MODE_MAP[reasoning_mode]
    return replace(
        config,
        thinking_type=thinking_type,
        reasoning_effort=reasoning_effort,
    )
