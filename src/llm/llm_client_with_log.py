from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from tqdm.auto import tqdm

from src.config import LLMConfig

CheckFunc = Callable[[str], bool]


class LLMClient:
    """
    更适合生产使用的 LLM 接口层：
    1. 单例化 ChatOpenAI 客户端
    2. 支持单轮、多轮、多 session
    3. 支持 check_func 校验
    4. 支持 async 批量并发
    5. 支持 prompt/response JSONL 日志
    """

    def __init__(
        self,
        config: LLMConfig,
        logger: Optional[logging.Logger] = None,
        log_path: Optional[str | Path] = None,
    ) -> None:
        self.config = config
        self.logger = logger or self._build_default_logger()
        self.log_path = self._resolve_log_path(log_path)
        self._log_lock = threading.Lock()

        self.llm = ChatOpenAI(
            model=config.model,
            openai_api_key=config.api_key,
            openai_api_base=config.base_url,
            temperature=config.temperature,
            top_p=config.top_p,
            timeout=config.timeout,
            max_retries=config.max_retries,
            max_completion_tokens=config.max_completion_tokens,
        )

        self._sessions: dict[str, list[BaseMessage]] = {}
        self.logger.info("LLM interaction log path: %s", self.log_path)

    @staticmethod
    def _build_default_logger() -> logging.Logger:
        logger = logging.getLogger("LLMClientWithLog")
        if not logger.handlers:
            logger.setLevel(logging.INFO)
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "[%(asctime)s] [%(levelname)s] %(message)s",
                    datefmt="%H:%M:%S",
                )
            )
            logger.addHandler(handler)
        return logger

    @staticmethod
    def _backoff_seconds(base: float, attempt: int) -> float:
        return base * (2 ** attempt) + random.random() * 0.25

    @staticmethod
    def _to_text(message: BaseMessage | str) -> str:
        if isinstance(message, str):
            return message
        content = getattr(message, "content", "")
        if isinstance(content, str):
            return content
        return str(content)

    @staticmethod
    def _serialize_message(message: BaseMessage) -> dict[str, str]:
        return {
            "type": message.__class__.__name__,
            "content": LLMClient._to_text(message),
        }

    @staticmethod
    def _serialize_messages(messages: Sequence[BaseMessage]) -> list[dict[str, str]]:
        return [LLMClient._serialize_message(message) for message in messages]

    @staticmethod
    def _resolve_log_path(log_path: Optional[str | Path]) -> Path:
        if log_path is None:
            return Path(__file__).resolve().with_name("llm_interactions.jsonl")
        return Path(log_path).expanduser().resolve()

    def _write_log_record(self, record: dict[str, object]) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_lock:
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _log_interaction(
        self,
        *,
        mode: str,
        messages: Sequence[BaseMessage],
        response: str,
        session_id: Optional[str] = None,
        batch_id: Optional[str] = None,
        item_index: Optional[int] = None,
        error: Optional[str] = None,
    ) -> None:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "model": self.config.model,
            "mode": mode,
            "session_id": session_id,
            "batch_id": batch_id,
            "item_index": item_index,
            "prompt": self._serialize_messages(messages),
            "response": response,
            "error": error,
        }
        self._write_log_record(record)

    def new_session_id(self) -> str:
        return uuid.uuid4().hex

    def reset_session(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _get_session_messages(self, session_id: str) -> list[BaseMessage]:
        return self._sessions.setdefault(session_id, [])

    def _build_single_turn_messages(
        self,
        text: str,
        system_prompt: Optional[str] = None,
    ) -> list[BaseMessage]:
        return [
            SystemMessage(content=system_prompt or self.config.default_system_prompt),
            HumanMessage(content=text),
        ]

    def _build_multi_turn_messages(
        self,
        session_id: str,
        text: str,
        system_prompt: Optional[str] = None,
    ) -> list[BaseMessage]:
        history = self._get_session_messages(session_id)
        return [
            SystemMessage(content=system_prompt or self.config.default_system_prompt),
            *history,
            HumanMessage(content=text),
        ]

    def single_turn(
        self,
        text: str,
        *,
        system_prompt: Optional[str] = None,
        check_func: Optional[CheckFunc] = None,
        max_retry: int = 3,
        backoff_base: float = 1.0,
    ) -> str:
        if not text:
            return ""

        messages = self._build_single_turn_messages(text, system_prompt)

        for attempt in range(max_retry):
            try:
                response = self.llm.invoke(messages)
                output = self._to_text(response)

                if check_func is not None and not check_func(output):
                    raise ValueError("check_func returned False")

                self._log_interaction(
                    mode="single_turn",
                    messages=messages,
                    response=output,
                )
                return output
            except Exception as e:
                self.logger.warning(
                    "single_turn failed on attempt %s/%s: %s",
                    attempt + 1,
                    max_retry,
                    e,
                )
                if attempt < max_retry - 1:
                    time.sleep(self._backoff_seconds(backoff_base, attempt))
                else:
                    self._log_interaction(
                        mode="single_turn",
                        messages=messages,
                        response="",
                        error=str(e),
                    )
                    return ""

        return ""

    def chat(
        self,
        session_id: str,
        text: str,
        *,
        system_prompt: Optional[str] = None,
        check_func: Optional[CheckFunc] = None,
        max_retry: int = 3,
        backoff_base: float = 1.0,
    ) -> str:
        if not text:
            return ""

        for attempt in range(max_retry):
            try:
                messages = self._build_multi_turn_messages(session_id, text, system_prompt)
                response = self.llm.invoke(messages)
                output = self._to_text(response)

                if check_func is not None and not check_func(output):
                    raise ValueError("check_func returned False")

                history = self._get_session_messages(session_id)
                history.append(HumanMessage(content=text))
                history.append(AIMessage(content=output))
                self._log_interaction(
                    mode="chat",
                    messages=messages,
                    response=output,
                    session_id=session_id,
                )
                return output

            except Exception as e:
                self.logger.warning(
                    "chat failed for session=%s on attempt %s/%s: %s",
                    session_id,
                    attempt + 1,
                    max_retry,
                    e,
                )
                if attempt < max_retry - 1:
                    time.sleep(self._backoff_seconds(backoff_base, attempt))
                else:
                    failed_messages = self._build_multi_turn_messages(session_id, text, system_prompt)
                    self._log_interaction(
                        mode="chat",
                        messages=failed_messages,
                        response="",
                        session_id=session_id,
                        error=str(e),
                    )
                    return ""

        return ""

    async def _asingle_turn_with_retry(
        self,
        text: str,
        *,
        system_prompt: Optional[str] = None,
        check_func: Optional[CheckFunc] = None,
        max_retry: int = 3,
        backoff_base: float = 1.0,
        semaphore: asyncio.Semaphore,
        batch_id: Optional[str] = None,
        item_index: Optional[int] = None,
    ) -> str:
        if not text:
            return ""

        messages = self._build_single_turn_messages(text, system_prompt)

        for attempt in range(max_retry):
            try:
                async with semaphore:
                    response = await self.llm.ainvoke(messages)

                output = self._to_text(response)

                if check_func is not None and not check_func(output):
                    raise ValueError("check_func returned False")

                self._log_interaction(
                    mode="batch_single_turn" if batch_id else "async_single_turn",
                    messages=messages,
                    response=output,
                    batch_id=batch_id,
                    item_index=item_index,
                )
                return output

            except Exception as e:
                self.logger.warning(
                    "abatch item failed on attempt %s/%s: %s",
                    attempt + 1,
                    max_retry,
                    e,
                )
                if attempt < max_retry - 1:
                    await asyncio.sleep(self._backoff_seconds(backoff_base, attempt))
                else:
                    self._log_interaction(
                        mode="batch_single_turn" if batch_id else "async_single_turn",
                        messages=messages,
                        response="",
                        batch_id=batch_id,
                        item_index=item_index,
                        error=str(e),
                    )
                    return ""

        return ""

    async def abatch_single_turn(
        self,
        texts: Sequence[str],
        *,
        system_prompt: Optional[str] = None,
        check_func: Optional[CheckFunc] = None,
        max_retry: int = 3,
        backoff_base: float = 1.0,
        max_concurrency: Optional[int] = None,
        show_progress: bool = False,
        progress_desc: str = "LLM batch",
    ) -> list[str]:
        semaphore = asyncio.Semaphore(max_concurrency or self.config.max_concurrency)
        batch_id = uuid.uuid4().hex

        async def _run_one(idx: int, text: str) -> tuple[int, str]:
            result = await self._asingle_turn_with_retry(
                text,
                system_prompt=system_prompt,
                check_func=check_func,
                max_retry=max_retry,
                backoff_base=backoff_base,
                semaphore=semaphore,
                batch_id=batch_id,
                item_index=idx,
            )
            return idx, result

        tasks = [
            asyncio.create_task(_run_one(idx, text))
            for idx, text in enumerate(texts)
        ]

        results = [""] * len(tasks)

        if show_progress:
            with tqdm(total=len(tasks), desc=progress_desc) as pbar:
                for fut in asyncio.as_completed(tasks):
                    idx, output = await fut
                    results[idx] = output
                    pbar.update(1)
        else:
            for fut in asyncio.as_completed(tasks):
                idx, output = await fut
                results[idx] = output

        return results

    def batch_single_turn(
        self,
        texts: Sequence[str],
        *,
        system_prompt: Optional[str] = None,
        check_func: Optional[CheckFunc] = None,
        max_retry: int = 3,
        backoff_base: float = 1.0,
        max_concurrency: Optional[int] = None,
        show_progress: bool = False,
        progress_desc: str = "LLM batch",
    ) -> list[str]:
        return asyncio.run(
            self.abatch_single_turn(
                texts,
                system_prompt=system_prompt,
                check_func=check_func,
                max_retry=max_retry,
                backoff_base=backoff_base,
                max_concurrency=max_concurrency,
                show_progress=show_progress,
                progress_desc=progress_desc,
            )
        )
