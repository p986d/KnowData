__all__ = [
    "ER2DataRunner",
    "UnitTask",
    "ER2DataSQLCandidateRunner",
    "ER2DataSchemaLinkRunner",
]


def __getattr__(name: str):
    if name in {"ER2DataRunner", "UnitTask"}:
        from src.er2data.er2query import ER2DataRunner, UnitTask

        return {"ER2DataRunner": ER2DataRunner, "UnitTask": UnitTask}[name]
    if name in {"ER2DataSQLCandidateRunner", "ER2DataSchemaLinkRunner"}:
        from src.er2data.sql_candidates import (
            ER2DataSQLCandidateRunner,
            ER2DataSchemaLinkRunner,
        )

        return {
            "ER2DataSQLCandidateRunner": ER2DataSQLCandidateRunner,
            "ER2DataSchemaLinkRunner": ER2DataSchemaLinkRunner,
        }[name]
    raise AttributeError(name)
