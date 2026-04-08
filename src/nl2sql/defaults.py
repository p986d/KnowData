from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
WORKSPACE_ROOT = PROJECT_ROOT.parent

DEFAULT_ENGINE_PROVIDER = "reforce"
DEFAULT_REFORCE_ROOT = WORKSPACE_ROOT / "ReFoRCE"
DEFAULT_SPIDER2_ROOT = WORKSPACE_ROOT / "Spider2" / "spider2-snow"
DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT = (
    DEFAULT_REFORCE_ROOT / "methods" / "ReFoRCE" / "online_schema_linking.py"
)
DEFAULT_NL2SQL_ENGINE_SCRIPT = (
    DEFAULT_REFORCE_ROOT / "methods" / "ReFoRCE" / "online_nl2sql.py"
)
DEFAULT_PROVIDER_LOG_ROOT = PROJECT_ROOT / "log" / "engine_provider"

# Compatibility alias for legacy imports.
DEFAULT_ENGINE_SCRIPT = DEFAULT_SCHEMA_LINKING_ENGINE_SCRIPT
