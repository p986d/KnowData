from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage


def message_to_text(message: BaseMessage | str) -> str:
    if isinstance(message, str):
        return message

    reasoning_text = _extract_reasoning_text(message)
    answer_text = _extract_answer_text(message)

    if reasoning_text and answer_text:
        return (
            "<reasoning>\n"
            f"{reasoning_text}\n"
            "</reasoning>\n\n"
            "<answer>\n"
            f"{answer_text}\n"
            "</answer>"
        )
    if reasoning_text:
        return reasoning_text
    return answer_text


def _extract_reasoning_text(message: BaseMessage) -> str:
    reasoning_parts: list[str] = []

    content_blocks = getattr(message, "content_blocks", None)
    if isinstance(content_blocks, list):
        for block in content_blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") != "reasoning":
                continue
            reasoning = block.get("reasoning")
            if isinstance(reasoning, str) and reasoning.strip():
                reasoning_parts.append(reasoning.strip())

    if reasoning_parts:
        return "\n\n".join(reasoning_parts)

    additional_kwargs = getattr(message, "additional_kwargs", {})
    if isinstance(additional_kwargs, dict):
        reasoning_content = additional_kwargs.get("reasoning_content")
        if isinstance(reasoning_content, str):
            return reasoning_content.strip()

    return ""


def _extract_answer_text(message: BaseMessage) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        text_parts: list[str] = []
        for item in content:
            text = _extract_text_from_content_item(item)
            if text:
                text_parts.append(text)
        if text_parts:
            return "\n".join(text_parts).strip()
    return str(content).strip()


def _extract_text_from_content_item(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""

    item_type = item.get("type")
    if item_type == "reasoning":
        return ""

    for key in ("text", "content", "value"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""
