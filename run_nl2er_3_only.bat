@echo off
setlocal EnableExtensions

pushd "%~dp0" || exit /b 1

set "PYTHON_EXE=D:\Tools\MiniConda\envs\spider2\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=python"

@REM Choose `single` to run specified question_id values, or `all` to traverse every question in the input file.
set "RUN_MODE=all"

set "INPUT_PATH=%CD%\data\input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input.json"
@REM set "INPUT_PATH=%CD%\data\sy_input_with_hint.json"

@REM Multiple values can be comma-separated.
@REM set "QUESTION_IDS=sf_bq017"
@REM set "QUESTION_IDS=sf_bq248"
@REM set "QUESTION_IDS=sf_local157"
set "QUESTION_IDS=sy00"

set "LLM=deepseek_chat_2"

@REM Used only when RUN_MODE=all.
set "MAX_WORKERS=64"

if /I "%RUN_MODE%"=="single" goto run_single
if /I "%RUN_MODE%"=="all" goto run_all

echo [run_nl2er_3_only.bat] invalid RUN_MODE=%RUN_MODE%. Use `single` or `all`.
popd
exit /b 1

:run_single
"%PYTHON_EXE%" -m src.run.nl2er_3_only ^
  --input-path "%INPUT_PATH%" ^
  --question-id "%QUESTION_IDS%" ^
  --model-config "%LLM%"
goto after_run

:run_all
"%PYTHON_EXE%" -m src.run.nl2er_3_only ^
  --input-path "%INPUT_PATH%" ^
  --model-config "%LLM%" ^
  --max-workers %MAX_WORKERS%

:after_run
if errorlevel 1 (
  echo [run_nl2er_3_only.bat] nl2er_3 run failed.
  popd
  exit /b 1
)

popd
