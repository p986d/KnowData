from __future__ import annotations

import sys
from pathlib import Path

from src.config import load_settings
from src.nl2sql.base import EngineProvider, EngineRuntimeConfig
from src.nl2sql.defaults import (
    DEFAULT_ENGINE_PROVIDER,
    DEFAULT_NL2SQL_ENGINE_SCRIPT,
    DEFAULT_REFORCE_ROOT,
    DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT,
    DEFAULT_SPIDER2_ROOT,
)
from src.nl2sql.providers.reforce import ReforceEngineProvider


def _try_load_engine_settings():
    try:
        return load_settings().engine
    except Exception:
        return None


def resolve_engine_provider_name(provider_name: str | None = None) -> str:
    normalized = str(provider_name or "").strip()
    if normalized:
        return normalized

    engine_settings = _try_load_engine_settings()
    if engine_settings is not None:
        return engine_settings.default_provider
    return DEFAULT_ENGINE_PROVIDER


def get_engine_provider(provider_name: str | None = None) -> EngineProvider:
    resolved_name = resolve_engine_provider_name(provider_name)
    if resolved_name == "reforce":
        return ReforceEngineProvider()
    raise KeyError(f"Engine provider not found: {resolved_name}")


def resolve_engine_runtime(
    *,
    provider_name: str | None = None,
    spider2_root: str | Path | None = None,
    reforce_root: str | Path | None = None,
    engine_script: str | Path | None = None,
    nl2sql_engine_script: str | Path | None = None,
    python_executable: str | None = None,
) -> EngineRuntimeConfig:
    resolved_name = resolve_engine_provider_name(provider_name)
    legacy_overrides = {
        "spider2_root": spider2_root,
        "reforce_root": reforce_root,
        "engine_script": engine_script,
        "nl2sql_engine_script": nl2sql_engine_script,
    }

    if resolved_name != "reforce":
        used_legacy_keys = [
            key for key, value in legacy_overrides.items()
            if value is not None and str(value).strip()
        ]
        if used_legacy_keys:
            raise ValueError(
                "Legacy ReFoRCE overrides are only supported for the `reforce` provider: "
                + ", ".join(sorted(used_legacy_keys))
            )

    root = DEFAULT_REFORCE_ROOT
    schema_linking_script = DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT
    nl2sql_script = DEFAULT_NL2SQL_ENGINE_SCRIPT
    resolved_spider2_root = DEFAULT_SPIDER2_ROOT

    engine_settings = _try_load_engine_settings()
    if engine_settings is not None:
        provider_config = engine_settings.get_provider(resolved_name)
        root = Path(provider_config.root)
        schema_linking_script = Path(provider_config.schema_linking_script)
        nl2sql_script = Path(provider_config.nl2sql_script)
        resolved_spider2_root = Path(provider_config.spider2_root)

    if reforce_root is not None:
        root = Path(reforce_root)
    if engine_script is not None:
        schema_linking_script = Path(engine_script)
    if nl2sql_engine_script is not None:
        nl2sql_script = Path(nl2sql_engine_script)
    if spider2_root is not None:
        resolved_spider2_root = Path(spider2_root)

    return EngineRuntimeConfig(
        provider_name=resolved_name,
        root=root,
        spider2_root=resolved_spider2_root,
        schema_linking_script=schema_linking_script,
        nl2sql_script=nl2sql_script,
        python_executable=python_executable or sys.executable or "python",
    )
