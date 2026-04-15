from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_KNOWDATA_DATABASES_ROOT = PROJECT_ROOT / "databases"


@dataclass(slots=True, frozen=True)
class DatabaseResourceResolution:
    db_id: str
    source: str
    db_root: Path
    knowdata_candidate: Path
    reforce_candidate: Path

    def to_payload(self) -> dict[str, str]:
        return {
            "db_id": self.db_id,
            "source": self.source,
            "db_root": str(self.db_root),
            "knowdata_candidate": str(self.knowdata_candidate),
            "reforce_candidate": str(self.reforce_candidate),
        }


def _is_valid_database_root(path: Path) -> bool:
    if not path.exists() or not path.is_dir():
        return False

    has_json = any(path.rglob("*.json"))
    if not has_json:
        return False

    has_ddl = (path / "DDL.csv").exists() or any(path.rglob("DDL.csv"))
    return has_ddl


def resolve_database_resource(
    db_id: str,
    *,
    spider2_root: str | Path,
    knowdata_databases_root: str | Path = DEFAULT_KNOWDATA_DATABASES_ROOT,
) -> DatabaseResourceResolution:
    normalized_db_id = str(db_id or "").strip()
    if not normalized_db_id:
        raise ValueError("`db_id` is required to resolve database resources.")

    knowdata_candidate = Path(knowdata_databases_root).resolve() / normalized_db_id
    reforce_candidate = (
        Path(spider2_root).resolve() / "resource" / "databases" / normalized_db_id
    )

    if _is_valid_database_root(knowdata_candidate):
        return DatabaseResourceResolution(
            db_id=normalized_db_id,
            source="knowdata",
            db_root=knowdata_candidate,
            knowdata_candidate=knowdata_candidate,
            reforce_candidate=reforce_candidate,
        )

    if _is_valid_database_root(reforce_candidate):
        return DatabaseResourceResolution(
            db_id=normalized_db_id,
            source="reforce",
            db_root=reforce_candidate,
            knowdata_candidate=knowdata_candidate,
            reforce_candidate=reforce_candidate,
        )

    raise FileNotFoundError(
        "Database resources not found for "
        f"`{normalized_db_id}`. "
        f"Tried Knowdata: {knowdata_candidate} ; "
        f"Tried ReFoRCE: {reforce_candidate}"
    )
