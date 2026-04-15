from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from typing import Callable, Optional, Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from src.config import LLMConfig
from tqdm.auto import tqdm

CheckFunc = Callable[[str], bool]


class LLMClient:
    """
    一个更适合生产使用的 LLM 接口层：
    1. 单例化 ChatOpenAI 客户端
    2. 支持单轮、多轮、多 session
    3. 支持 check_func 校验
    4. 支持 async 批量并发
    """

    def __init__(self, config: LLMConfig, logger: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.logger = logger or self._build_default_logger()

        self.llm = ChatOpenAI(
            model=config.model,
            api_key=config.api_key,
            base_url=config.base_url,
            temperature=config.temperature,
            top_p=config.top_p,
            timeout=config.timeout,
            max_retries=config.max_retries,
            max_completion_tokens=config.max_completion_tokens,
        )

        self._sessions: dict[str, list[BaseMessage]] = {}

    @staticmethod
    def _build_default_logger() -> logging.Logger:
        logger = logging.getLogger("LLMClient")
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
    ) -> str:
        if not text:
            return ""

        messages = self._build_single_turn_messages(text, system_prompt)
        output = ""

        for attempt in range(max_retry):
            try:
                async with semaphore:
                    response = await self.llm.ainvoke(messages)
                    
                output = self._to_text(response)
                
                # print(output)
                if check_func is not None and not check_func(output):
                    raise ValueError("check_func returned False")

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

        async def _run_one(idx: int, text: str) -> tuple[int, str]:
            result = await self._asingle_turn_with_retry(
                text,
                system_prompt=system_prompt,
                check_func=check_func,
                max_retry=max_retry,
                backoff_base=backoff_base,
                semaphore=semaphore,
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
