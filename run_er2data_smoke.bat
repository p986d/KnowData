@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "DB_ID=NEW_YORK_CITIBIKE_1"
set "NL2ER_OUTPUT_PATH=D:\Workspace\Spider2Test\metadata\NEW_YORK_CITIBIKE_1_20260327-104407\nl2er_output.json"
set "QUESTION_MODEL_CONFIG=qwen3_30B_thinking"
set "NL2SQL_MODEL_CONFIG=qwen3_30B_thinking"
set "MAX_NL2SQL_WORKERS=64"
set "METADATA_DIR=%CD%\metadata\er2data_smoke"
set "LOG_DIR=%CD%\log\er2data\er2data_smoke"

python -m src.run.er2data ^
  --input-path "%NL2ER_OUTPUT_PATH%" ^
  --db-id "%DB_ID%" ^
  --question-model-config "%QUESTION_MODEL_CONFIG%" ^
  --nl2sql-model-config "%NL2SQL_MODEL_CONFIG%" ^
  --max-nl2sql-workers %MAX_NL2SQL_WORKERS% ^
  --skip-nl2sql ^
  --metadata-dir "%METADATA_DIR%" ^
  --log-dir "%LOG_DIR%"
if errorlevel 1 (
  echo [run_er2data_smoke.bat] er2data smoke run failed.
  popd
  exit /b 1
)

popd
