@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"

@REM Set METADATA_RUN to a run folder under metadata, or set METADATA_PATH directly.
set "METADATA_RUN=20260603-164353"
set "METADATA_PATH=%CD%\metadata\%METADATA_RUN%"
set "OUTPUT_DIR=%METADATA_PATH%\buildtopic"

set "LLM=deepseek_v4_flash"
@REM Supported values: non_think, think_high, think_max
set "REASONING_MODE=non_think"
set "MAX_ATTRIBUTE_WORKERS=8"

if not exist "%METADATA_PATH%" (
  echo [run_knowdata_buildtopic.bat] metadata path not found: %METADATA_PATH%
  popd
  exit /b 1
)

"%PYTHON_EXE%" -B -m src.buildtopic.pipeline ^
  --metadata-path "%METADATA_PATH%" ^
  --output-dir "%OUTPUT_DIR%" ^
  --model-config "%LLM%" ^
  --reasoning-mode "%REASONING_MODE%" ^
  --max-attribute-workers %MAX_ATTRIBUTE_WORKERS%

if errorlevel 1 (
  echo [run_knowdata_buildtopic.bat] buildtopic run failed.
  popd
  exit /b 1
)

echo [run_knowdata_buildtopic.bat] buildtopic output wrote to %OUTPUT_DIR%
popd
exit /b 0
