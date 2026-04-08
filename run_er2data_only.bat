@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "QUESTION_ID=sf_bq050"
set "DB_ID=NEW_YORK_CITIBIKE_1"
set "NL2ER_OUTPUT_PATH=D:\Workspace\Knowdata\metadata\sf_bq050_20260330-164222\nl2er_output.json"
set "QUESTION_MODEL_CONFIG=deepseek_chat"
set "NL2SQL_MODEL_CONFIG=deepseek_chat"
set "MAX_NL2SQL_WORKERS=64"
set "METADATA_DIR=%CD%\metadata\sf_bq050_20260330-164222"
set "LOG_DIR=%CD%\log\er2data\er2data_only_test"

python -m src.run.er2data ^
  --input-path "%NL2ER_OUTPUT_PATH%" ^
  --question-id "%QUESTION_ID%" ^
  --db-id "%DB_ID%" ^
  --question-model-config "%QUESTION_MODEL_CONFIG%" ^
  --nl2sql-model-config "%NL2SQL_MODEL_CONFIG%" ^
  --max-nl2sql-workers %MAX_NL2SQL_WORKERS% ^
  --metadata-dir "%METADATA_DIR%" ^
  --log-dir "%LOG_DIR%"
if errorlevel 1 (
  echo [run_er2data_only.bat] er2data run failed.
  popd
  exit /b 1
)

popd
