from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Optional, Union

import yaml
from dotenv import load_dotenv

from src.nl2sql.defaults import (
    DEFAULT_ENGINE_PROVIDER,
    DEFAULT_NL2SQL_ENGINE_SCRIPT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT,
    DEFAULT_SPIDER2_ROOT,
)

from .schema import (
    EngineProviderConfig,
    EngineSettings,
    LLMConfig,
    LLMSettings,
    MySQLConfig,
    Settings,
    SnowflakeConfig,
)


CONFIG_DIR = Path(__file__).resolve().parent
SRC_DIR = CONFIG_DIR.parent
PROJECT_ROOT = SRC_DIR.parent

DEFAULT_CONFIG_PATH = CONFIG_DIR / "base.yaml"
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"


def _load_yaml(path: Union[str, Path]) -> dict[str, Any]:
    path = Path(path)
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _resolve_project_path(value: Union[str, Path]) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path.resolve()
    return (PROJECT_ROOT / path).resolve()


ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _get_env_backed_value(raw_value: Optional[str]) -> Optional[str]:
    if not raw_value:
        return None
    if ENV_NAME_PATTERN.fullmatch(raw_value):
        return os.environ.get(raw_value, raw_value)
    return raw_value


def _get_env_backed_int(raw_value: Optional[str]) -> Optional[int]:
    value = _get_env_backed_value(raw_value)
    if value in (None, ""):
        return None
    return int(value)


def load_settings(
    config_path: Optional[Union[str, Path]] = None,
    env_path: Optional[Union[str, Path]] = None,
) -> Settings:
    config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
    env_path = Path(env_path) if env_path else DEFAULT_ENV_PATH

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    load_dotenv(env_path)

    raw = _load_yaml(config_path)

    # -------------------------
    # Snowflake
    # -------------------------
    snowflake_raw = raw.get("snowflake", {})
    snowflake = SnowflakeConfig(
        account=os.environ.get("SNOWFLAKE_ACCOUNT", ""),
        user=os.environ.get("SNOWFLAKE_USER", ""),
        password=os.environ.get("SNOWFLAKE_PASSWORD", ""),
        role=snowflake_raw.get("role"),
        warehouse=snowflake_raw.get("warehouse"),
        database=snowflake_raw.get("database"),
        schema=snowflake_raw.get("schema"),
    )

    # -------------------------
    # MySQL
    # -------------------------
    mysql_sy_test_raw = raw.get("mysql_sy_test", {})
    mysql_sy_test = MySQLConfig(
        host=_get_env_backed_value(mysql_sy_test_raw.get("host")),
        port=_get_env_backed_int(mysql_sy_test_raw.get("port")),
        user=_get_env_backed_value(mysql_sy_test_raw.get("user")),
        password=_get_env_backed_value(mysql_sy_test_raw.get("password")),
        database=mysql_sy_test_raw.get("database"),
    )

    # -------------------------
    # LLM
    # -------------------------
    llm_raw = raw.get("llm", {})
    default_model = llm_raw.get("default_model", "model_1")
    model_raw_map = llm_raw.get("models", {})

    llm_models: dict[str, LLMConfig] = {}
    for model_name, cfg in model_raw_map.items():
        api_key_env = cfg.get("api_key_env")
        base_url_env = cfg.get("base_url_env")

        if not api_key_env:
            raise ValueError(f"llm.models.{model_name}.api_key_env is required")
        if not base_url_env:
            raise ValueError(f"llm.models.{model_name}.base_url_env is required")

        if api_key_env not in os.environ:
            raise KeyError(
                f"Environment variable not found for llm model '{model_name}': {api_key_env}"
            )
        if base_url_env not in os.environ:
            raise KeyError(
                f"Environment variable not found for llm model '{model_name}': {base_url_env}"
            )

        llm_models[model_name] = LLMConfig(
            api_key=os.environ[api_key_env],
            base_url=os.environ[base_url_env],
            model=cfg["model"],
            temperature=cfg.get("temperature", 0.1),
            top_p=cfg.get("top_p", 0.95),
            timeout=cfg.get("timeout", 60.0),
            max_retries=cfg.get("max_retries", 2),
            max_completion_tokens=cfg.get("max_completion_tokens", 4096),
            max_concurrency=cfg.get("max_concurrency", 8),
            default_system_prompt=cfg.get(
                "default_system_prompt",
                "You are a helpful assistant.",
            ),
        )

    llm = LLMSettings(
        default_model=default_model,
        models=llm_models,
    )

    # -------------------------
    # Engine providers
    # -------------------------
    engine_raw = raw.get("engine", {})
    provider_raw_map = engine_raw.get("providers", {})
    engine_provider_map: dict[str, EngineProviderConfig] = {}

    for provider_name, provider_cfg in provider_raw_map.items():
        engine_provider_map[provider_name] = EngineProviderConfig(
            root=str(_resolve_project_path(provider_cfg["root"])),
            spider2_root=str(_resolve_project_path(provider_cfg["spider2_root"])),
            nl2sql_script=str(_resolve_project_path(provider_cfg["nl2sql_script"])),
            schema_linking_script=str(
                _resolve_project_path(provider_cfg["schema_linking_script"])
            ),
        )

    if "reforce" not in engine_provider_map:
        engine_provider_map["reforce"] = EngineProviderConfig(
            root=str(DEFAULT_REFORCE_ROOT),
            spider2_root=str(DEFAULT_SPIDER2_ROOT),
            nl2sql_script=str(DEFAULT_NL2SQL_ENGINE_SCRIPT),
            schema_linking_script=str(DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT),
        )

    engine = EngineSettings(
        default_provider=engine_raw.get("default_provider", DEFAULT_ENGINE_PROVIDER),
        providers=engine_provider_map,
    )

    return Settings(
        snowflake=snowflake,
        mysql_sy_test=mysql_sy_test,
        llm=llm,
        engine=engine,
    )
