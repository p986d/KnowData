@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

@REM Point this to either a metadata batch directory or a single case directory.
set "METADATA_DIR=%CD%\metadata\20260426-181526"
set "METADATA_DIR=%CD%\metadata\20260426-181526\sf_local030_20260426-181526"
@REM set "METADATA_DIR=%CD%\metadata\20260426-181526\sf_local157_20260426-181526"

set "LLM=deepseek_chat_personal"
set "MAX_BATCH_WORKERS=8"
set "MAX_UNIT_CONCURRENCY=64"
set "SPIDER2_ROOT=D:\Workspace\ReFoRCE\spider2-snow"
set "SAMPLE_VALUES_PER_COLUMN=5"

@REM Set to true to only generate prompts and placeholder outputs without calling the LLM.
set "DRY_RUN=false"

set "EXTRA_ARGS="
if /I "%DRY_RUN%"=="true" set "EXTRA_ARGS=--dry-run"

python -m src.run.nl2er_refine_0 ^
  --metadata-dir "%METADATA_DIR%" ^
  --model-config "%LLM%" ^
  --spider2-root "%SPIDER2_ROOT%" ^
  --sample-values-per-column %SAMPLE_VALUES_PER_COLUMN% ^
  --max-batch-workers %MAX_BATCH_WORKERS% ^
  --max-unit-concurrency %MAX_UNIT_CONCURRENCY% ^
  %EXTRA_ARGS%
if errorlevel 1 (
  echo [run_nl2er_refine_0.bat] nl2er_refine_0 run failed.
  popd
  exit /b 1
)

popd
