import io
import json
import shutil
import uuid
from pathlib import Path
from types import SimpleNamespace

from src.nl2sql.base import EngineRuntimeConfig, NL2SQLRequest
from src.nl2sql.providers import reforce
from src.nl2sql.providers.reforce_online_nl2sql import validate_backend_args


class FakeProcess:
    def __init__(self, command: list[str], **_kwargs: object) -> None:
        self.command = command
        self.stdout = io.StringIO("")
        self.stderr = io.StringIO("")
        self.returncode = 0

        output_path = Path(command[command.index("--output_path") + 1])
        output_path.write_text(
            json.dumps(
                {
                    "sql_candidates": [
                        "SELECT 1 AS value;",
                        "SELECT 2 AS value;",
                    ]
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        schema_linking_path = (
            output_path.parent / f"{output_path.stem}_artifacts" / "schema_linking.json"
        )
        schema_linking_path.parent.mkdir(parents=True, exist_ok=True)
        schema_linking_path.write_text(
            json.dumps(
                {
                    "linked_tables": ["DB.SCHEMA.TABLE"],
                    "linked_columns": ["DB.SCHEMA.TABLE.value"],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        self.returncode = -9


def test_reforce_nl2sql_candidate_only_skips_execution_credentials(
    monkeypatch,
) -> None:
    tmp_path = Path("tests/_tmp") / f"reforce_candidate_{uuid.uuid4().hex}"
    tmp_path.mkdir(parents=True)
    captured_command: list[str] = []

    def fake_popen(command: list[str], **kwargs: object) -> FakeProcess:
        captured_command[:] = command
        return FakeProcess(command, **kwargs)

    class FakeLLMSettings:
        def get(self, _name: str | None = None) -> SimpleNamespace:
            return SimpleNamespace(
                api_key="test-key",
                base_url="https://example.invalid/v1",
                model="test-model",
            )

    try:
        monkeypatch.setattr(
            reforce,
            "load_settings",
            lambda: SimpleNamespace(llm=FakeLLMSettings()),
        )
        monkeypatch.setattr(
            reforce,
            "_resolve_database_binding",
            lambda _runtime, _db_id: (tmp_path / "db_root", "test"),
        )
        monkeypatch.setattr(
            reforce,
            "resolve_database_execution_profile",
            lambda db_id: SimpleNamespace(
                db_id=db_id,
                backend="snowflake",
                dialect="snowflake",
            ),
        )
        monkeypatch.setattr(reforce.subprocess, "Popen", fake_popen)

        output_path = tmp_path / "candidate_result.json"
        result = reforce.ReforceEngineProvider().run_nl2sql(
            NL2SQLRequest(
                question="Find values.",
                db_id="TEST_DB",
                output_path=str(output_path),
                return_candidates_only=True,
            ),
            runtime=EngineRuntimeConfig(
                provider_name="reforce_gen_sl_m1",
                root=tmp_path,
                spider2_root=tmp_path / "spider2",
                nl2sql_script=tmp_path / "online_nl2sql_gen_sl_m1.py",
                python_executable="python",
            ),
        )

        assert result.ok is True
        assert result.sql == ""
        assert result.sql_candidates == [
            "SELECT 1 AS value;",
            "SELECT 2 AS value;",
        ]
        assert result.tables == [
            {
                "fullname": "DB.SCHEMA.TABLE",
                "database": "DB",
                "schema": "SCHEMA",
                "table": "TABLE",
            }
        ]
        assert "--return_candidates_only" in captured_command
        assert "--snowflake_account" not in captured_command
    finally:
        shutil.rmtree(tmp_path, ignore_errors=True)


def test_candidate_only_wrapper_validation_does_not_require_execution_credentials() -> None:
    backend, dialect = validate_backend_args(
        SimpleNamespace(
            return_candidates_only=True,
            backend="snowflake",
            dialect=None,
            snowflake_account=None,
            snowflake_user=None,
            snowflake_password=None,
            mysql_host=None,
            mysql_port=None,
            mysql_user=None,
            mysql_password=None,
            mysql_database=None,
        )
    )

    assert (backend, dialect) == ("snowflake", "snowflake")
