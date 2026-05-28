@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"
set "DB_ID=sy_community_link"
set "DATABASE_ROOT=%CD%\databases"
set "LOG_ROOT=%CD%\log\database_data_concept"
set "OUTPUT_FILENAME=database_data_concept.json"

set "LLM=deepseek_v4_flash"
set "REASONING_MODE=non_think"
set "MAX_CONCURRENCY=4"
set "SAMPLE_VALUES_PER_COLUMN=5"

@REM Set DRY_RUN=1 to validate files and prompts without calling the LLM.
set "DRY_RUN=0"
@REM Optional comma-separated or space-separated full table names.
set "TABLE_FULLNAME="

set "DRY_RUN_ARG="
if "%DRY_RUN%"=="1" set "DRY_RUN_ARG=--dry-run"

set "TABLE_ARG="
if not "%TABLE_FULLNAME%"=="" set "TABLE_ARG=--table-fullname %TABLE_FULLNAME%"

"%PYTHON_EXE%" -B -m src.run.database_data_concept ^
  --db-id "%DB_ID%" ^
  --database-root "%DATABASE_ROOT%" ^
  --log-root "%LOG_ROOT%" ^
  --output-filename "%OUTPUT_FILENAME%" ^
  --model-config "%LLM%" ^
  --reasoning-mode "%REASONING_MODE%" ^
  --max-concurrency %MAX_CONCURRENCY% ^
  --sample-values-per-column %SAMPLE_VALUES_PER_COLUMN% ^
  %TABLE_ARG% ^
  %DRY_RUN_ARG%

if errorlevel 1 (
  echo [run_db_concept.bat] database data concept run failed.
  popd
  exit /b 1
)

popd
exit /b 0
