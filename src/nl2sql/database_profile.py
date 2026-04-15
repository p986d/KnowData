from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True, frozen=True)
class DatabaseExecutionProfile:
    db_id: str
    backend: str
    dialect: str
    connection_name: str | None = None


_DATABASE_EXECUTION_PROFILES: dict[str, DatabaseExecutionProfile] = {
    "sy_community_link": DatabaseExecutionProfile(
        db_id="sy_community_link",
        backend="mysql",
        dialect="mysql",
        connection_name="mysql_sy_test",
    ),
}


def resolve_database_execution_profile(db_id: str) -> DatabaseExecutionProfile:
    normalized_db_id = str(db_id or "").strip()
    if not normalized_db_id:
        raise ValueError("`db_id` is required to resolve database execution profile.")
    return _DATABASE_EXECUTION_PROFILES.get(
        normalized_db_id,
        DatabaseExecutionProfile(
            db_id=normalized_db_id,
            backend="snowflake",
            dialect="snowflake",
            connection_name="snowflake",
        ),
    )
