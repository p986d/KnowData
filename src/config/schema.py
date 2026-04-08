from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(slots=True)
class SnowflakeConfig:
    account: str
    user: str
    password: str
    role: Optional[str] = None
    warehouse: Optional[str] = None
    database: Optional[str] = None
    schema: Optional[str] = None

@dataclass(slots=True)
class LLMConfig:
    api_key: str
    base_url: str
    model: str
    temperature: float = 0.1
    top_p: float = 0.95
    timeout: float = 60.0
    max_retries: int = 2
    max_completion_tokens: int = 4096
    max_concurrency: int = 8
    default_system_prompt: str = "You are a helpful assistant."


@dataclass(slots=True)
class LLMSettings:
    default_model: str
    models: dict[str, LLMConfig] = field(default_factory=dict)

    def get(self, name: str | None = None) -> LLMConfig:
        model_name = name or self.default_model
        if model_name not in self.models:
            raise KeyError(f"LLM model config not found: {model_name}")
        return self.models[model_name]


@dataclass(slots=True)
class EngineProviderConfig:
    root: str
    spider2_root: str
    nl2sql_script: str
    schema_linking_script: str


@dataclass(slots=True)
class EngineSettings:
    default_provider: str
    providers: dict[str, EngineProviderConfig] = field(default_factory=dict)

    def get_provider(self, name: str | None = None) -> EngineProviderConfig:
        provider_name = name or self.default_provider
        if provider_name not in self.providers:
            raise KeyError(f"Engine provider config not found: {provider_name}")
        return self.providers[provider_name]


@dataclass(slots=True)
class Settings:
    snowflake: SnowflakeConfig
    llm: LLMSettings
    engine: EngineSettings
